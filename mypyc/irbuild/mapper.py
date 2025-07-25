"""Maintain a mapping from mypy concepts to IR/compiled concepts."""

from __future__ import annotations

from mypy.nodes import ARG_STAR, ARG_STAR2, GDEF, ArgKind, FuncDef, RefExpr, SymbolNode, TypeInfo
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    LiteralType,
    NoneTyp,
    Overloaded,
    PartialType,
    TupleType,
    Type,
    TypedDictType,
    TypeType,
    TypeVarLikeType,
    UnboundType,
    UninhabitedType,
    UnionType,
    find_unpack_in_list,
    get_proper_type,
)
from mypyc.ir.class_ir import ClassIR
from mypyc.ir.func_ir import FuncDecl, FuncSignature, RuntimeArg
from mypyc.ir.rtypes import (
    RInstance,
    RTuple,
    RType,
    RUnion,
    bool_rprimitive,
    bytes_rprimitive,
    dict_rprimitive,
    float_rprimitive,
    frozenset_rprimitive,
    int16_rprimitive,
    int32_rprimitive,
    int64_rprimitive,
    int_rprimitive,
    list_rprimitive,
    none_rprimitive,
    object_rprimitive,
    range_rprimitive,
    set_rprimitive,
    str_rprimitive,
    tuple_rprimitive,
    uint8_rprimitive,
)


class Mapper:
    """Keep track of mappings from mypy concepts to IR concepts.

    For example, we keep track of how the mypy TypeInfos of compiled
    classes map to class IR objects.

    This state is shared across all modules being compiled in all
    compilation groups.
    """

    def __init__(self, group_map: dict[str, str | None]) -> None:
        self.group_map = group_map
        self.type_to_ir: dict[TypeInfo, ClassIR] = {}
        self.func_to_decl: dict[SymbolNode, FuncDecl] = {}
        self.symbol_fullnames: set[str] = set()
        # The corresponding generator class that implements a generator/async function
        self.fdef_to_generator: dict[FuncDef, ClassIR] = {}

    def type_to_rtype(self, typ: Type | None) -> RType:
        if typ is None:
            return object_rprimitive

        typ = get_proper_type(typ)
        if isinstance(typ, Instance):
            if typ.type.fullname == "builtins.int":
                return int_rprimitive
            elif typ.type.fullname == "builtins.float":
                return float_rprimitive
            elif typ.type.fullname == "builtins.bool":
                return bool_rprimitive
            elif typ.type.fullname == "builtins.str":
                return str_rprimitive
            elif typ.type.fullname == "builtins.bytes":
                return bytes_rprimitive
            elif typ.type.fullname == "builtins.list":
                return list_rprimitive
            # Dict subclasses are at least somewhat common and we
            # specifically support them, so make sure that dict operations
            # get optimized on them.
            elif any(cls.fullname == "builtins.dict" for cls in typ.type.mro):
                return dict_rprimitive
            elif typ.type.fullname == "builtins.set":
                return set_rprimitive
            elif typ.type.fullname == "builtins.frozenset":
                return frozenset_rprimitive
            elif typ.type.fullname == "builtins.tuple":
                return tuple_rprimitive  # Varying-length tuple
            elif typ.type.fullname == "builtins.range":
                return range_rprimitive
            elif typ.type in self.type_to_ir:
                inst = RInstance(self.type_to_ir[typ.type])
                # Treat protocols as Union[protocol, object], so that we can do fast
                # method calls in the cases where the protocol is explicitly inherited from
                # and fall back to generic operations when it isn't.
                if typ.type.is_protocol:
                    return RUnion([inst, object_rprimitive])
                else:
                    return inst
            elif typ.type.fullname == "mypy_extensions.i64":
                return int64_rprimitive
            elif typ.type.fullname == "mypy_extensions.i32":
                return int32_rprimitive
            elif typ.type.fullname == "mypy_extensions.i16":
                return int16_rprimitive
            elif typ.type.fullname == "mypy_extensions.u8":
                return uint8_rprimitive
            else:
                return object_rprimitive
        elif isinstance(typ, TupleType):
            # Use our unboxed tuples for raw tuples but fall back to
            # being boxed for NamedTuple or for variadic tuples.
            if (
                typ.partial_fallback.type.fullname == "builtins.tuple"
                and find_unpack_in_list(typ.items) is None
            ):
                return RTuple([self.type_to_rtype(t) for t in typ.items])
            else:
                return tuple_rprimitive
        elif isinstance(typ, CallableType):
            return object_rprimitive
        elif isinstance(typ, NoneTyp):
            return none_rprimitive
        elif isinstance(typ, UnionType):
            return RUnion.make_simplified_union([self.type_to_rtype(item) for item in typ.items])
        elif isinstance(typ, AnyType):
            return object_rprimitive
        elif isinstance(typ, TypeType):
            return object_rprimitive
        elif isinstance(typ, TypeVarLikeType):
            # Erase type variable to upper bound.
            # TODO: Erase to union if object has value restriction?
            return self.type_to_rtype(typ.upper_bound)
        elif isinstance(typ, PartialType):
            assert typ.var.type is not None
            return self.type_to_rtype(typ.var.type)
        elif isinstance(typ, Overloaded):
            return object_rprimitive
        elif isinstance(typ, TypedDictType):
            return dict_rprimitive
        elif isinstance(typ, LiteralType):
            return self.type_to_rtype(typ.fallback)
        elif isinstance(typ, (UninhabitedType, UnboundType)):
            # Sure, whatever!
            return object_rprimitive

        # I think we've covered everything that is supposed to
        # actually show up, so anything else is a bug somewhere.
        assert False, "unexpected type %s" % type(typ)

    def get_arg_rtype(self, typ: Type, kind: ArgKind) -> RType:
        if kind == ARG_STAR:
            return tuple_rprimitive
        elif kind == ARG_STAR2:
            return dict_rprimitive
        else:
            return self.type_to_rtype(typ)

    def fdef_to_sig(self, fdef: FuncDef, strict_dunders_typing: bool) -> FuncSignature:
        if isinstance(fdef.type, CallableType):
            arg_types = [
                self.get_arg_rtype(typ, kind)
                for typ, kind in zip(fdef.type.arg_types, fdef.type.arg_kinds)
            ]
            arg_pos_onlys = [name is None for name in fdef.type.arg_names]
            # TODO: We could probably support decorators sometimes (static and class method?)
            if (fdef.is_coroutine or fdef.is_generator) and not fdef.is_decorated:
                # Give a more precise type for generators, so that we can optimize
                # code that uses them. They return a generator object, which has a
                # specific class. Without this, the type would have to be 'object'.
                ret: RType = RInstance(self.fdef_to_generator[fdef])
            else:
                ret = self.type_to_rtype(fdef.type.ret_type)
        else:
            # Handle unannotated functions
            arg_types = [object_rprimitive for _ in fdef.arguments]
            arg_pos_onlys = [arg.pos_only for arg in fdef.arguments]
            # We at least know the return type for __init__ methods will be None.
            is_init_method = fdef.name == "__init__" and bool(fdef.info)
            if is_init_method:
                ret = none_rprimitive
            else:
                ret = object_rprimitive

        # mypyc FuncSignatures (unlike mypy types) want to have a name
        # present even when the argument is position only, since it is
        # the sole way that FuncDecl arguments are tracked. This is
        # generally fine except in some cases (like for computing
        # init_sig) we need to produce FuncSignatures from a
        # deserialized FuncDef that lacks arguments. We won't ever
        # need to use those inside of a FuncIR, so we just make up
        # some crap.
        if hasattr(fdef, "arguments"):
            arg_names = [arg.variable.name for arg in fdef.arguments]
        else:
            arg_names = [name or "" for name in fdef.arg_names]

        args = [
            RuntimeArg(arg_name, arg_type, arg_kind, arg_pos_only)
            for arg_name, arg_kind, arg_type, arg_pos_only in zip(
                arg_names, fdef.arg_kinds, arg_types, arg_pos_onlys
            )
        ]

        if not strict_dunders_typing:
            # We force certain dunder methods to return objects to support letting them
            # return NotImplemented. It also avoids some pointless boxing and unboxing,
            # since tp_richcompare needs an object anyways.
            # However, it also prevents some optimizations.
            if fdef.name in ("__eq__", "__ne__", "__lt__", "__gt__", "__le__", "__ge__"):
                ret = object_rprimitive

        return FuncSignature(args, ret)

    def is_native_module(self, module: str) -> bool:
        """Is the given module one compiled by mypyc?"""
        return module in self.group_map

    def is_native_ref_expr(self, expr: RefExpr) -> bool:
        if expr.node is None:
            return False
        if "." in expr.node.fullname:
            name = expr.node.fullname.rpartition(".")[0]
            return self.is_native_module(name) or name in self.symbol_fullnames
        return True

    def is_native_module_ref_expr(self, expr: RefExpr) -> bool:
        return self.is_native_ref_expr(expr) and expr.kind == GDEF
