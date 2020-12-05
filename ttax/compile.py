"""Utils for compiling functions defined as einsum strings.

Here we use the notion of *tt_einsum*, which is similar to einsum strings, but
with more structure.

Basic element of tt_einsum is a *tt_einsum_core*: list with three elements,
which defines an einsum string for a single TT-core. First element is indices
for the left TT-rank, second element is the indices for the main dimensions of
the resulting TT-core, and the last element is the indices for the right
TT-rank.

TT_einsum consists of list of input and output cores defined like with
tt_einsum_cores.
"""

import opt_einsum as oe
import numpy as np
import jax.numpy as jnp
from string import ascii_lowercase
import copy

from ttax.base_class import TT


class WrappedTT:
  """A class which wraps TT, which is needed for fusion to work.

  Base TT class can only have jnp.array objects so that you can pass it into
  jitted function. But, for fusing two functions together we need to track which
  operation created a TT object, so while fusing ops we wrap TT objects with
  this class, to track that.
  """
  def __init__(self, tt: TT, inputs=None, tt_einsum=None):
    self.tt = tt
    self.inputs = inputs
    self.tt_einsum = tt_einsum

  @property
  def tt_cores(self):
    return self.tt.tt_cores

  @property
  def batch_shape(self):
    return self.tt.batch_shape

  @property
  def shape(self):
    return self.tt.shape

  @property
  def axis_dim(self):
    return self.tt.axis_dim

  @property
  def num_batch_dims(self):
    return self.tt.num_batch_dims


def tt_to_vanilla_einsum(tt_einsum):
  """Converting from tt_einsum to a regular einsum."""
  args = []
  for arg in tt_einsum['args']:
    args.append(''.join(arg))
  return ','.join(['...' + a for a in args]) + '->...' + ''.join(tt_einsum['res'])


def compile(func):
  """Decorator converting tt einsum definition into a function.
  
  Example:
  @compile
  def multiply(a, b):
    return {
             'type': 'independent',
             'args': [['a', 'i', 'b'], ['c', 'i', 'd']],
             'res': ['ac', 'i', 'bd']
           }
  """
  tt_einsum = func(None, None)  # TODO: infer desired number of arguments.
  if tt_einsum['type'] == 'independent':
    return compile_independent(tt_einsum)
  elif tt_einsum['type'] == 'running':
    return compile_running(tt_einsum)
  else:
    raise ValueError('Unsupported tt_einsum type "%s"' % tt_einsum['type'])


def compile_independent(tt_einsum):
  def new_func(*args):
    is_fusing = any([isinstance(tt, WrappedTT) for tt in args])
    if is_fusing:
      # Have to use a different name to make upper level tt_einsum visible.
      tt_einsum_, args = _fuse(tt_einsum, args)
    else:
      tt_einsum_ = tt_einsum
    einsum = tt_to_vanilla_einsum(tt_einsum_)
    num_batch_dims = args[0].num_batch_dims
    # TODO: support broadcasting.
    res_batch_shape = list(args[0].batch_shape)
    # TODO: do in parallel w.r.t. cores.
    # TODO: use optimal einsum.
    res_cores = []
    for i in range(len(args[0].tt_cores)):
      curr_input_cores = [tt.tt_cores[i] for tt in args]
      core = oe.contract(einsum, *curr_input_cores, backend='jax')
      shape = core.shape[num_batch_dims:]
      num_left_rank_dims = len(tt_einsum_['res'][0])
      num_tensor_dims = len(tt_einsum_['res'][1])
      split_points = (num_left_rank_dims, num_left_rank_dims + num_tensor_dims)
      new_shape = np.split(shape, split_points)
      new_shape = [np.prod(s) for s in new_shape]
      new_shape = res_batch_shape + new_shape
      res_cores.append(core.reshape(new_shape))
    res = TT(res_cores)
    if is_fusing:
      res = WrappedTT(res, args, tt_einsum)
    return res
  return new_func


def compile_running(tt_einsum):
  def new_func(*args):
    is_fusing = any([isinstance(tt, WrappedTT) for tt in args])
    if is_fusing:
      # Have to use a different name to make upper level tt_einsum visible.
      tt_einsum_, args = _fuse(tt_einsum, args)
    else:
      tt_einsum_ = tt_einsum
    einsum = tt_to_vanilla_einsum(tt_einsum_)

    res = jnp.ones([1] * len(args), args[0].tt_cores[0].dtype)
    res_list = []
    for core_idx in range(len(args[0].tt_cores)):
      curr_tensors = [a.tt_cores[core_idx] for a in args]
      curr_tensors.append(res)
      # TODO: use optimal einsum.
      res = oe.contract(einsum, *curr_tensors, backend='jax', optimize='optimal')
      res_list.append(res)
    return res_list
  return new_func


def fuse(func):
  """Fuse a composite function to make it faster.

  Example:
    def f(a, b, c):
      # Computes
      # <a * b, c> =
      #   sum_{i_1, ..., i_d} a[i_1, ..., i_d] b[i_1, ..., i_d] c[i_1, ..., i_d]
      return ttax.flat_inner(a * b, c)

  Function `f` can be suboptimal for some inputs. For example, if `a` and `b`
  are of large TT-rank, and `c` is of low TT-rank, implementing the same
  operation as
    ttax.flat_inner(a * c, b)
  would be much more efficient.

  `fuse` automates such optimizations. You can build an optimal implementation
  of this function for any inputs by doing
    faster_f = ttax.fuse(f)
  Finally, don't forget that in Jax to get good speed you need to wrap you
  highest level function in jit, e.g.
    faster_f = jax.jit(faster_f)
  Now, by using `faster_f(a, b, c)` instead of `f(a, b, c)` you can achieve
  a much faster running time for any inputs.
  """
  def _func(*args):
    wrapped_args = [WrappedTT(arg) for arg in args]
    res = func(*wrapped_args)
    if isinstance(res, WrappedTT):
      res = res.tt
    return res
  return _func


def _fuse(tt_einsum, args):
  tt_einsum = copy.deepcopy(tt_einsum)
  new_tt_einsum = {'args': [], 'res': tt_einsum['res'], 'type': tt_einsum['type']}
  new_args = []
  for arg_idx, arg in enumerate(args):
    if isinstance(arg, WrappedTT) and arg.tt_einsum is not None:
      assert arg.tt_einsum['type'] == 'independent'
      einsum = tt_to_vanilla_einsum(tt_einsum)
      # TODO: add upper case
      vacant_letters = [l for l in ascii_lowercase if l not in einsum]
      curr_einsum = tt_to_vanilla_einsum(arg.tt_einsum)
      curr_unique_letters = [l for l in curr_einsum if l in ascii_lowercase]
      mapping = {}
      for i, l in enumerate(curr_unique_letters):
        mapping[l] = vacant_letters[i]
      new_curr_tt_einsum = apply_mapping(arg.tt_einsum, mapping)

      mapping = {}
      for fr, to in zip(tt_einsum['args'][arg_idx], new_curr_tt_einsum['res']):
        if len(fr) == 1:
          mapping[fr] = to
        elif len(fr) == len(to):
          for i in range(len(fr)):
            mapping[fr[i]] = to[i]
        else:
          raise ValueError()
      new_res_ = apply_mapping({'args': tt_einsum['args'], 'res': new_tt_einsum['res']}, mapping)
      new_tt_einsum['args'] += new_curr_tt_einsum['args']
      tt_einsum['args'] = new_res_['args']
      new_tt_einsum['res'] = new_res_['res']
      new_args += arg.inputs
    else:
      new_tt_einsum['args'].append(tt_einsum['args'][arg_idx])
      new_args.append(arg)
  new_tt_einsum['args'] += tt_einsum['args'][len(args):]
  return new_tt_einsum, new_args


def apply_single_mapping(strings, mapping):
  new_strings = []
  for str in strings:
    curr_str = ''
    for l in str:
      curr_str += mapping.get(l, l)
    new_strings.append(curr_str)
  return new_strings


def apply_mapping(tt_einsum, mapping):
  new_args = []
  for arg in tt_einsum['args']:
    new_args.append(apply_single_mapping(arg, mapping))
  tt_einsum['args'] = new_args
  tt_einsum['res'] = apply_single_mapping(tt_einsum['res'], mapping)
  return tt_einsum
