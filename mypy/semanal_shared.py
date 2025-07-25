"""Shared definitions used by different parts of semantic analysis."""

from __future__ import annotations

from abc import abstractmethod
from typing import Callable, Final, Literal, Protocol, overload

from mypy_extensions import trait

from mypy.errorcodes import LITERAL_REQ, ErrorCode
from mypy.nodes import (
    CallExpr,
    ClassDef,
    Context,
    DataclassTransformSpec,
    Decorator,
    Expression,
    FuncDef,
    NameExpr,
    Node,
    OverloadedFuncDef,
    RefExpr,
    SymbolNode,
    SymbolTable,
    SymbolTableNode,
    TypeInfo,
)
from mypy.plugin import SemanticAnalyzerPluginInterface
from mypy.tvar_scope import TypeVarLikeScope
from mypy.type_visitor import ANY_STRATEGY, BoolTypeQuery
from mypy.typeops import make_simplified_union
from mypy.types import (
    TPDICT_FB_NAMES,
    AnyType,
    FunctionLike,
    Instance,
    Parameters,
    ParamSpecFlavor,
    ParamSpecType,
    PlaceholderType,
    ProperType,
    TupleType,
    Type,
    TypeOfAny,
    TypeVarId,
    TypeVarLikeType,
    TypeVarTupleType,
    UnpackType,
    flatten_nested_tuples,
    get_proper_type,
)

# Subclasses can override these Var attributes with incompatible types. This can also be
# set for individual attributes using 'allow_incompatible_override' of Var.
ALLOW_INCOMPATIBLE_OVERRIDE: Final = ("__slots__", "__deletable__", "__match_args__")


# Priorities for ordering of patches within the "patch" phase of semantic analysis
# (after the main pass):

# Fix fallbacks (does subtype checks).
PRIORITY_FALLBACKS: Final = 1


@trait
class SemanticAnalyzerCoreInterface:
    """A core abstract interface to generic semantic analyzer functionality.

    This is implemented by both semantic analyzer passes 2 and 3.
    """

    @abstractmethod
    def lookup_qualified(
        self, name: str, ctx: Context, suppress_errors: bool = False
    ) -> SymbolTableNode | None:
        raise NotImplementedError

    @abstractmethod
    def lookup_fully_qualified(self, fullname: str, /) -> SymbolTableNode:
        raise NotImplementedError

    @abstractmethod
    def lookup_fully_qualified_or_none(self, fullname: str, /) -> SymbolTableNode | None:
        raise NotImplementedError

    @abstractmethod
    def fail(
        self,
        msg: str,
        ctx: Context,
        serious: bool = False,
        *,
        blocker: bool = False,
        code: ErrorCode | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def note(self, msg: str, ctx: Context, *, code: ErrorCode | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def incomplete_feature_enabled(self, feature: str, ctx: Context) -> bool:
        raise NotImplementedError

    @abstractmethod
    def record_incomplete_ref(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def defer(self, debug_context: Context | None = None, force_progress: bool = False) -> None:
        raise NotImplementedError

    @abstractmethod
    def is_incomplete_namespace(self, fullname: str) -> bool:
        """Is a module or class namespace potentially missing some definitions?"""
        raise NotImplementedError

    @property
    @abstractmethod
    def final_iteration(self) -> bool:
        """Is this the final iteration of semantic analysis?"""
        raise NotImplementedError

    @abstractmethod
    def is_future_flag_set(self, flag: str) -> bool:
        """Is the specific __future__ feature imported"""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_stub_file(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def is_func_scope(self) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def type(self) -> TypeInfo | None:
        raise NotImplementedError


@trait
class SemanticAnalyzerInterface(SemanticAnalyzerCoreInterface):
    """A limited abstract interface to some generic semantic analyzer pass 2 functionality.

    We use this interface for various reasons:

    * Looser coupling
    * Cleaner import graph
    * Less need to pass around callback functions
    """

    tvar_scope: TypeVarLikeScope

    @abstractmethod
    def lookup(
        self, name: str, ctx: Context, suppress_errors: bool = False
    ) -> SymbolTableNode | None:
        raise NotImplementedError

    @abstractmethod
    def named_type(self, fullname: str, args: list[Type] | None = None) -> Instance:
        raise NotImplementedError

    @abstractmethod
    def named_type_or_none(self, fullname: str, args: list[Type] | None = None) -> Instance | None:
        raise NotImplementedError

    @abstractmethod
    def accept(self, node: Node) -> None:
        raise NotImplementedError

    @abstractmethod
    def anal_type(
        self,
        typ: Type,
        /,
        *,
        tvar_scope: TypeVarLikeScope | None = None,
        allow_tuple_literal: bool = False,
        allow_unbound_tvars: bool = False,
        allow_typed_dict_special_forms: bool = False,
        allow_placeholder: bool = False,
        report_invalid_types: bool = True,
        prohibit_self_type: str | None = None,
        prohibit_special_class_field_types: str | None = None,
    ) -> Type | None:
        raise NotImplementedError

    @abstractmethod
    def get_and_bind_all_tvars(self, type_exprs: list[Expression]) -> list[TypeVarLikeType]:
        raise NotImplementedError

    @abstractmethod
    def basic_new_typeinfo(self, name: str, basetype_or_fallback: Instance, line: int) -> TypeInfo:
        raise NotImplementedError

    @abstractmethod
    def schedule_patch(self, priority: int, patch: Callable[[], None]) -> None:
        raise NotImplementedError

    @abstractmethod
    def add_symbol_table_node(self, name: str, symbol: SymbolTableNode) -> bool:
        """Add node to the current symbol table."""
        raise NotImplementedError

    @abstractmethod
    def current_symbol_table(self) -> SymbolTable:
        """Get currently active symbol table.

        May be module, class, or local namespace.
        """
        raise NotImplementedError

    @abstractmethod
    def add_symbol(
        self,
        name: str,
        node: SymbolNode,
        context: Context,
        module_public: bool = True,
        module_hidden: bool = False,
        can_defer: bool = True,
    ) -> bool:
        """Add symbol to the current symbol table."""
        raise NotImplementedError

    @abstractmethod
    def add_symbol_skip_local(self, name: str, node: SymbolNode) -> None:
        """Add symbol to the current symbol table, skipping locals.

        This is used to store symbol nodes in a symbol table that
        is going to be serialized (local namespaces are not serialized).
        See implementation docstring for more details.
        """
        raise NotImplementedError

    @abstractmethod
    def parse_bool(self, expr: Expression) -> bool | None:
        raise NotImplementedError

    @abstractmethod
    def qualified_name(self, name: str) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_typeshed_stub_file(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def process_placeholder(
        self, name: str | None, kind: str, ctx: Context, force_progress: bool = False
    ) -> None:
        raise NotImplementedError


def set_callable_name(sig: Type, fdef: FuncDef) -> ProperType:
    sig = get_proper_type(sig)
    if isinstance(sig, FunctionLike):
        if fdef.info:
            if fdef.info.fullname in TPDICT_FB_NAMES:
                # Avoid exposing the internal _TypedDict name.
                class_name = "TypedDict"
            else:
                class_name = fdef.info.name
            return sig.with_name(f"{fdef.name} of {class_name}")
        else:
            return sig.with_name(fdef.name)
    else:
        return sig


def calculate_tuple_fallback(typ: TupleType) -> None:
    """Calculate a precise item type for the fallback of a tuple type.

    This must be called only after the main semantic analysis pass, since joins
    aren't available before that.

    Note that there is an apparent chicken and egg problem with respect
    to verifying type arguments against bounds. Verifying bounds might
    require fallbacks, but we might use the bounds to calculate the
    fallbacks. In practice this is not a problem, since the worst that
    can happen is that we have invalid type argument values, and these
    can happen in later stages as well (they will generate errors, but
    we don't prevent their existence).
    """
    fallback = typ.partial_fallback
    assert fallback.type.fullname == "builtins.tuple"
    items = []
    for item in flatten_nested_tuples(typ.items):
        # TODO: this duplicates some logic in typeops.tuple_fallback().
        if isinstance(item, UnpackType):
            unpacked_type = get_proper_type(item.type)
            if isinstance(unpacked_type, TypeVarTupleType):
                unpacked_type = get_proper_type(unpacked_type.upper_bound)
            if (
                isinstance(unpacked_type, Instance)
                and unpacked_type.type.fullname == "builtins.tuple"
            ):
                items.append(unpacked_type.args[0])
            else:
                raise NotImplementedError
        else:
            items.append(item)
    fallback.args = (make_simplified_union(items),)


class _NamedTypeCallback(Protocol):
    def __call__(self, fullname: str, args: list[Type] | None = None) -> Instance: ...


def paramspec_args(
    name: str,
    fullname: str,
    id: TypeVarId,
    *,
    named_type_func: _NamedTypeCallback,
    line: int = -1,
    column: int = -1,
    prefix: Parameters | None = None,
) -> ParamSpecType:
    return ParamSpecType(
        name,
        fullname,
        id,
        flavor=ParamSpecFlavor.ARGS,
        upper_bound=named_type_func("builtins.tuple", [named_type_func("builtins.object")]),
        default=AnyType(TypeOfAny.from_omitted_generics),
        line=line,
        column=column,
        prefix=prefix,
    )


def paramspec_kwargs(
    name: str,
    fullname: str,
    id: TypeVarId,
    *,
    named_type_func: _NamedTypeCallback,
    line: int = -1,
    column: int = -1,
    prefix: Parameters | None = None,
) -> ParamSpecType:
    return ParamSpecType(
        name,
        fullname,
        id,
        flavor=ParamSpecFlavor.KWARGS,
        upper_bound=named_type_func(
            "builtins.dict", [named_type_func("builtins.str"), named_type_func("builtins.object")]
        ),
        default=AnyType(TypeOfAny.from_omitted_generics),
        line=line,
        column=column,
        prefix=prefix,
    )


class HasPlaceholders(BoolTypeQuery):
    def __init__(self) -> None:
        super().__init__(ANY_STRATEGY)

    def visit_placeholder_type(self, t: PlaceholderType) -> bool:
        return True


def has_placeholder(typ: Type) -> bool:
    """Check if a type contains any placeholder types (recursively)."""
    return typ.accept(HasPlaceholders())


def find_dataclass_transform_spec(node: Node | None) -> DataclassTransformSpec | None:
    """
    Find the dataclass transform spec for the given node, if any exists.

    Per PEP 681 (https://peps.python.org/pep-0681/#the-dataclass-transform-decorator), dataclass
    transforms can be specified in multiple ways, including decorator functions and
    metaclasses/base classes. This function resolves the spec from any of these variants.
    """

    # The spec only lives on the function/class definition itself, so we need to unwrap down to that
    # point
    if isinstance(node, CallExpr):
        # Like dataclasses.dataclass, transform-based decorators can be applied either with or
        # without parameters; ie, both of these forms are accepted:
        #
        # @typing.dataclass_transform
        # class Foo: ...
        # @typing.dataclass_transform(eq=True, order=True, ...)
        # class Bar: ...
        #
        # We need to unwrap the call for the second variant.
        node = node.callee

    if isinstance(node, RefExpr):
        node = node.node

    if isinstance(node, Decorator):
        # typing.dataclass_transform usage must always result in a Decorator; it always uses the
        # `@dataclass_transform(...)` syntax and never `@dataclass_transform`
        node = node.func

    if isinstance(node, OverloadedFuncDef):
        # The dataclass_transform decorator may be attached to any single overload, so we must
        # search them all.
        # Note that using more than one decorator is undefined behavior, so we can just take the
        # first that we find.
        for candidate in node.items:
            spec = find_dataclass_transform_spec(candidate)
            if spec is not None:
                return spec
        return find_dataclass_transform_spec(node.impl)

    # For functions, we can directly consult the AST field for the spec
    if isinstance(node, FuncDef):
        return node.dataclass_transform_spec

    if isinstance(node, ClassDef):
        node = node.info
    if isinstance(node, TypeInfo):
        # Search all parent classes to see if any are decorated with `typing.dataclass_transform`
        for base in node.mro[1:]:
            if base.dataclass_transform_spec is not None:
                return base.dataclass_transform_spec

        # Check if there is a metaclass that is decorated with `typing.dataclass_transform`
        #
        # Note that PEP 681 only discusses using a metaclass that is directly decorated with
        # `typing.dataclass_transform`; subclasses thereof should be treated with dataclass
        # semantics rather than as transforms:
        #
        # > If dataclass_transform is applied to a class, dataclass-like semantics will be assumed
        # > for any class that directly or indirectly derives from the decorated class or uses the
        # > decorated class as a metaclass.
        #
        # The wording doesn't make this entirely explicit, but Pyright (the reference
        # implementation for this PEP) only handles directly-decorated metaclasses.
        metaclass_type = node.metaclass_type
        if metaclass_type is not None and metaclass_type.type.dataclass_transform_spec is not None:
            return metaclass_type.type.dataclass_transform_spec

    return None


# Never returns `None` if a default is given
@overload
def require_bool_literal_argument(
    api: SemanticAnalyzerInterface | SemanticAnalyzerPluginInterface,
    expression: Expression,
    name: str,
    default: Literal[True, False],
) -> bool: ...


@overload
def require_bool_literal_argument(
    api: SemanticAnalyzerInterface | SemanticAnalyzerPluginInterface,
    expression: Expression,
    name: str,
    default: None = None,
) -> bool | None: ...


def require_bool_literal_argument(
    api: SemanticAnalyzerInterface | SemanticAnalyzerPluginInterface,
    expression: Expression,
    name: str,
    default: bool | None = None,
) -> bool | None:
    """Attempt to interpret an expression as a boolean literal, and fail analysis if we can't."""
    value = parse_bool(expression)
    if value is None:
        api.fail(
            f'"{name}" argument must be a True or False literal', expression, code=LITERAL_REQ
        )
        return default

    return value


def parse_bool(expr: Expression) -> bool | None:
    if isinstance(expr, NameExpr):
        if expr.fullname == "builtins.True":
            return True
        if expr.fullname == "builtins.False":
            return False
    return None
