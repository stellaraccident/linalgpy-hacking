"""Model classes representing a tensor-comprehension."""

from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from mlir import ir as _ir

from .affine import *
from .types import *

# Type aliases.
AffineDimList = Dict[str, _ir.AffineExpr]
ShapeCoercable = Optional[Union[_ir.AffineMap, Sequence[AffineExprDef]]]


class Expression:
  """An expression that can appear on the RHS of a comprehension."""

  def visit_affine_exprs(self, callback):
    """Visits all affine expressions reachable by the expression."""
    pass

  def _get_all_dim_defs(self) -> Set[DimDef]:
    """Recursively gets all DimDef affine expressions that are referenced."""
    results = set()

    def visitor(affine_expr):
      if isinstance(affine_expr, DimDef):
        results.add(affine_expr)

    self.visit_affine_exprs(visitor)
    return results

  def __add__(self, rhs) -> "Expression":
    return Prim.add(self, rhs)

  def __mul__(self, rhs) -> "Expression":
    return Prim.mul(self, rhs)

  def __sub__(self, rhs) -> "Expression":
    return Prim.sub(self, rhs)


class TensorUse(Expression):
  """A used tensor represented by its (tensor_name, indices).

  Note that forming a comprehension via direct assignment is performed through
  __setitem__ on the TensorDef level. However, performing a reduction with
  compound ops (+=, *=, etc) is done by doing a:
    TensorDef.__getitem__
    TensorUse.__iadd__
    TensorDef.__setitem__
  """

  def __init__(self, tensor_def: "TensorDef", indices: Sequence[AffineExprDef]):
    self.tensor_def = tensor_def
    self.indices = tuple(indices)

  @property
  def tensor_name(self) -> str:
    n = self.tensor_def.tensor_name
    assert n is not None, "TensorDef not attached"
    return n

  def visit_affine_exprs(self, callback):
    for ind in self.indices:
      ind.visit_affine_exprs(callback)

  def __iadd__(self, rhs: Expression) -> Expression:
    return Reduce.add(*self._compute_reduce_dims(rhs))(rhs)

  def _compute_reduce_dims(self, rhs: Expression) -> Set[DimDef]:
    """For implicit reductions, computes default reduction dims.

    Assumes that the rhs is the expression being reduced and self is being
    reduced into. Any indices referenced on the rhs and not in self are
    considered reduction dims and will be ordered as encountered on the rhs.
    """
    rhs_dims = rhs._get_all_dim_defs()
    lhs_dims = self._get_all_dim_defs()
    return rhs_dims - lhs_dims

  def __repr__(self):
    return f"{self.tensor_name}[{', '.join([repr(i) for i in self.indices])}]"


class TensorDef:
  """Bookkeeping of a single registered tensor, held in dict by name."""

  def __init__(self,
               type_var: TypeVar,
               *shape: ShapeCoercable,
               indexing_map: Optional[_ir.AffineMap] = None,
               output: bool = False):
    if not isinstance(type_var, TypeVar):
      raise ValueError(f"TensorDef requires a TypeVar. Got: {repr(type_var)}")
    self.owner = None
    self.type_var = type_var
    self.shape = shape
    self.indexing_map = indexing_map
    self.output = output
    self.tensor_name = None
    self.registered_index = None  # Optional[int]

  def attach(self, index: int, tensor_name: str, owner: "TcOpDef"):
    if self.owner:
      raise ValueError(f"TensorDef already registered with op: {self}")
    self.registered_index = index
    self.tensor_name = tensor_name
    self.owner = owner

    # And do fixups that can only be done once attached.
    # if self.shape:
    #   self.shape = self.owner._coerce_to_shape(self.shape)

  def __getitem__(self, dims) -> TensorUse:
    state = AffineBuildState(global_state=self.owner._affine_state,
                             allow_new_symbols=False)
    if not isinstance(dims, tuple):
      dims = (dims,)  # Handle single subscript case.
    exprs = []
    for expr_def in dims:
      if not isinstance(expr_def, AffineExprDef):
        raise KeyError(
            "A TensorDef can only be subscripted by a tuple of affine dims")
      exprs.append(expr_def)
    # indexing_map = _ir.AffineMap.get(dim_count=state.dim_count,
    #                                  symbol_count=state.symbol_count,
    #                                  exprs=exprs)
    return TensorUse(self, exprs)

  def __setitem__(self, dims, value):
    """Creates a new 1:1 comprehension by binding this tensor to an expression.

    Note that due to the way assignment works in Python, we have to capture
    direct assignment as a setitem on the TensorDef.
    """
    if not isinstance(value, Expression):
      raise ValueError(f"Only Expressions can be assigned to TensorDefs. "
                       f"Got: {repr(value)}")
    use = self[dims]
    comp = Comprehension((use, value))
    self.owner.comprehensions.append(comp)

  def __repr__(self):
    output = "OUTPUT " if self.output else ""
    return (f"{self.tensor_name}:TensorDef({output}{repr(self.type_var)}, "
            f"shape={self.shape})")


class Comprehension:
  """Represents a single comprehension."""

  def __init__(self, *bindings: Tuple[TensorUse, Expression]):
    self.definitions = [d for d, _ in bindings]
    self.values = [v for _, v in bindings]

  def __repr__(self):
    if len(self.definitions) > 1:
      defs_repr = f"({', '.join(repr(d) for d in self.definitions)})"
      values_repr = f"({', '.join(repr(v) for v in self.values)})"
    else:
      defs_repr = f"{repr(self.definitions[0])}"
      values_repr = f"{repr(self.values[0])}"

    return f"{defs_repr} = {values_repr}"


class Prim:
  """Primitive operations."""

  def __init__(self, prim_name: str):
    self.prim_name = prim_name

  def __call__(self, *args):
    return PrimApply(self, args)

  def reduce(self, *reduce_dims: AffineExprDef):
    """Shortcut to create a Reduce operation from this primitive."""
    return Reduce(self, *reduce_dims)

  def __repr__(self):
    return f"{self.prim_name}"


class Reduce:
  """A reduction operator that reduces into its LHS from its RHS."""

  def __init__(self, operator: Prim, *reduce_dims: AffineExprDef):
    if not isinstance(operator, Prim):
      raise ValueError(f"Reduce expected a Prim operator. Got: {operator}")
    self.operator = operator
    self.reduce_dims = tuple(reduce_dims)

  def __call__(self, *args: Expression):
    return ReduceApply(self, args)

  def __repr__(self):
    return (f"reduce_{self.operator.prim_name}"
            f"({', '.join(repr(d) for d in self.reduce_dims)})")


# Built-in primitives and reductions.
Prim.add = Prim("add")
Reduce.add = Prim.add.reduce
Prim.exp = Prim("exp")
Prim.log = Prim("log")
Prim.mul = Prim("mul")
Reduce.mul = Prim.mul.reduce
Prim.max = Prim("max")
Reduce.max = Prim.max.reduce
Prim.sub = Prim("sub")

class PrimApply(Expression):
  """Application of a primitive."""

  def __init__(self, prim: Prim, args: Expression):
    self.prim = prim
    self.args = tuple(args)

  def visit_affine_exprs(self, callback):
    for arg in self.args:
      arg.visit_affine_exprs(callback)

  def __repr__(self):
    return f"{repr(self.prim)}({', '.join(repr(a) for a in self.args)})"


class ReduceApply(Expression):
  """Application of a reduction.

  Note that this only captures the reduction indices and the right hand side.
  It is assumed that it is applied to a LHS external to the expression.
  """

  def __init__(self, reduce: Reduce, args: Sequence[Expression]):
    self.reduce = reduce
    self.args = tuple(args)

  def visit_affine_exprs(self, callback):
    for ind in self.reduce.reduce_dims:
      ind.visit_affine_exprs(callback)
    for arg in self.args:
      arg.visit_affine_exprs(callback)

  def __repr__(self):
    return f"{repr(self.reduce)}({', '.join(repr(a) for a in self.args)})"


class TcOpDef:
  """Definition of a named op.

    >>> with _ir.Context():
    ...   od = TcOpDef('matmul')
    ...   A, B, C = od.add_tensor(
    ...     A=TensorDef('f32', shape=(S.M, S.K)),
    ...     B=TensorDef('f32', shape=(S.M, S.N)),
    ...     C=TensorDef('f32', shape=(S.M, S.N), output=True))
    ...   _ = od.add_comprehension(A[D.n, D.m])
    ...   od
    TcOpDef(matmul -> matmul,
      A:TensorDef(type_pred=f32, shape=()[s0, s1] -> (s0, s1))
      B:TensorDef(type_pred=f32, shape=()[s0, s1] -> (s0, s2))
      C:TensorDef(OUTPUT type_pred=f32, shape=()[s0, s1] -> (s0, s2))
  """

  def __init__(self, name: str, cpp_op_name: str = None):
    self.name = name
    self.cpp_op_name = cpp_op_name if cpp_op_name is not None else name
    self.registered_tensors = dict()  # type: Dict[str, TensorDef]
    self.comprehensions = list()  # type: List[Comprehension]
    self._affine_state = AffineBuildState()

  def add_tensor(self, tensor_name: str, tensor: TensorDef):
    """Registers a tensor."""
    if tensor_name in self.registered_tensors:
      raise ValueError(f"Tensor {tensor_name} is already registered "
                       f"to {self.registered_tensors['tensor_name']}")
    tensor.attach(len(self.registered_tensors), tensor_name, self)
    self.registered_tensors[tensor_name] = tensor

  def tensor(self, name):
    """Gets a registered tensor by name."""
    try:
      return self.registered_tensors[name]
    except KeyError:
      raise KeyError(f"Tensor {name} is not registered")

  def _coerce_to_shape(self, shape_spec: ShapeCoercable) -> _ir.AffineMap:
    state = AffineBuildState(global_state=self._affine_state,
                             allow_new_dims=False)
    if isinstance(shape_spec, _ir.AffineMap):
      return shape_spec
    # Treat as a sequence of affine expressions.
    exprs = []
    for expr_def in shape_spec:
      if not isinstance(expr_def, AffineExprDef):
        raise ValueError(
            f"Expected shape dim to be an AffineExprDef. Got {expr_def}")
      exprs.append(expr_def.build(state=state))
    assert state.dim_count == 0
    return _ir.AffineMap.get(dim_count=0,
                             symbol_count=state.symbol_count,
                             exprs=exprs)

  def __repr__(self):
    lines = [f"TcOpDef({self.name} -> {self.cpp_op_name},"]
    for name, tensor in self.registered_tensors.items():
      lines.append(f"  {tensor}")
    if self.comprehensions:
      lines[-1] += " {"
      for comprehension in self.comprehensions:
        lines.append(f"    {comprehension}")
      lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
  import doctest
  doctest.testmod()