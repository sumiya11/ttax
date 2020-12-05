import functools
import jax
import jax.numpy as jnp

from ttax.base_class import TT
from ttax.compile import compile


def tt_vmap(func):
  """Decorator which makes a function support batch TT-inputs."""
  @functools.wraps(func)
  def vectorized_func(*args, **kwargs):
    tt_arg = args[0]  # TODO: what if only kwargs are present?
    if tt_arg.num_batch_dims == 0:
      return func(*args, **kwargs)
    else:
      # Vmap everything num_batch_dims times.
      vmapped = func
      for _ in range(tt_arg.num_batch_dims):
        vmapped = jax.vmap(vmapped)
      return vmapped(*args, **kwargs)
  return vectorized_func


@tt_vmap
def full(tt: TT) -> jnp.array:
  """Converts TT into a regular tensor.
  """
  num_dims = len(tt.tt_cores)

  dtype = tt.tt_cores[0].dtype  # TODO: make tt.dtype.
  res = jnp.ones((1, 1), dtype=dtype)
  for i in range(num_dims):
    curr_core = tt.tt_cores[i]
    right_rank = curr_core.shape[-1]
    res = jnp.einsum('pa,aib->pib', res, curr_core)
    res = res.reshape(-1, right_rank)

  return jnp.reshape(res, tt.shape)


@compile
def multiply(a, b):
  return {
      'type': 'independent',
      'args': [['a', 'i', 'b'], ['c', 'i', 'd']],
      'res': ['ac', 'i', 'bd']
  }


def flat_inner(a, b):
  @compile
  def main_loop(a, b):
    return {
        'type': 'running',
        'args': [['a', 'i', 'b'], ['c', 'i', 'd'], ['a', 'c']],
        'res': ['b', 'd']
    }
  res = jnp.squeeze(main_loop(a, b)[-1])
  return res
