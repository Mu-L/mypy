"""Special case IR generation of calls to specific builtin functions.

Most special cases should be handled using the data driven "primitive
ops" system, but certain operations require special handling that has
access to the AST/IR directly and can make decisions/optimizations
based on it. These special cases can be implemented here.

For example, we use specializers to statically emit the length of a
fixed length tuple and to emit optimized code for any()/all() calls with
generator comprehensions as the argument.

See comment below for more documentation.
"""

from __future__ import annotations

from typing import Callable, Final, Optional

from mypy.nodes import (
    ARG_NAMED,
    ARG_POS,
    BytesExpr,
    CallExpr,
    DictExpr,
    Expression,
    GeneratorExpr,
    IntExpr,
    ListExpr,
    MemberExpr,
    NameExpr,
    RefExpr,
    StrExpr,
    TupleExpr,
)
from mypy.types import AnyType, TypeOfAny
from mypyc.ir.ops import (
    BasicBlock,
    Extend,
    Integer,
    RaiseStandardError,
    Register,
    Truncate,
    Unreachable,
    Value,
)
from mypyc.ir.rtypes import (
    RInstance,
    RPrimitive,
    RTuple,
    RType,
    bool_rprimitive,
    c_int_rprimitive,
    dict_rprimitive,
    int16_rprimitive,
    int32_rprimitive,
    int64_rprimitive,
    int_rprimitive,
    is_bool_rprimitive,
    is_dict_rprimitive,
    is_fixed_width_rtype,
    is_float_rprimitive,
    is_int16_rprimitive,
    is_int32_rprimitive,
    is_int64_rprimitive,
    is_int_rprimitive,
    is_list_rprimitive,
    is_uint8_rprimitive,
    list_rprimitive,
    set_rprimitive,
    str_rprimitive,
    uint8_rprimitive,
)
from mypyc.irbuild.builder import IRBuilder
from mypyc.irbuild.for_helpers import (
    comprehension_helper,
    sequence_from_generator_preallocate_helper,
    translate_list_comprehension,
    translate_set_comprehension,
)
from mypyc.irbuild.format_str_tokenizer import (
    FormatOp,
    convert_format_expr_to_str,
    join_formatted_strings,
    tokenizer_format_call,
)
from mypyc.primitives.bytes_ops import isinstance_bytearray, isinstance_bytes
from mypyc.primitives.dict_ops import (
    dict_items_op,
    dict_keys_op,
    dict_setdefault_spec_init_op,
    dict_values_op,
    isinstance_dict,
)
from mypyc.primitives.float_ops import isinstance_float
from mypyc.primitives.int_ops import isinstance_int
from mypyc.primitives.list_ops import isinstance_list, new_list_set_item_op
from mypyc.primitives.misc_ops import isinstance_bool
from mypyc.primitives.set_ops import isinstance_frozenset, isinstance_set
from mypyc.primitives.str_ops import (
    isinstance_str,
    str_encode_ascii_strict,
    str_encode_latin1_strict,
    str_encode_utf8_strict,
)
from mypyc.primitives.tuple_ops import isinstance_tuple, new_tuple_set_item_op

# Specializers are attempted before compiling the arguments to the
# function.  Specializers can return None to indicate that they failed
# and the call should be compiled normally. Otherwise they should emit
# code for the call and return a Value containing the result.
#
# Specializers take three arguments: the IRBuilder, the CallExpr being
# compiled, and the RefExpr that is the left hand side of the call.
Specializer = Callable[["IRBuilder", CallExpr, RefExpr], Optional[Value]]

# Dictionary containing all configured specializers.
#
# Specializers can operate on methods as well, and are keyed on the
# name and RType in that case.
specializers: dict[tuple[str, RType | None], list[Specializer]] = {}


def _apply_specialization(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr, name: str | None, typ: RType | None = None
) -> Value | None:
    # TODO: Allow special cases to have default args or named args. Currently they don't since
    #       they check that everything in arg_kinds is ARG_POS.

    # If there is a specializer for this function, try calling it.
    # Return the first successful one.
    if name and (name, typ) in specializers:
        for specializer in specializers[name, typ]:
            val = specializer(builder, expr, callee)
            if val is not None:
                return val
    return None


def apply_function_specialization(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr
) -> Value | None:
    """Invoke the Specializer callback for a function if one has been registered"""
    return _apply_specialization(builder, expr, callee, callee.fullname)


def apply_method_specialization(
    builder: IRBuilder, expr: CallExpr, callee: MemberExpr, typ: RType | None = None
) -> Value | None:
    """Invoke the Specializer callback for a method if one has been registered"""
    name = callee.fullname if typ is None else callee.name
    return _apply_specialization(builder, expr, callee, name, typ)


def specialize_function(
    name: str, typ: RType | None = None
) -> Callable[[Specializer], Specializer]:
    """Decorator to register a function as being a specializer.

    There may exist multiple specializers for one function. When
    translating method calls, the earlier appended specializer has
    higher priority.
    """

    def wrapper(f: Specializer) -> Specializer:
        specializers.setdefault((name, typ), []).append(f)
        return f

    return wrapper


@specialize_function("builtins.globals")
def translate_globals(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) == 0:
        return builder.load_globals_dict()
    return None


@specialize_function("builtins.abs")
@specialize_function("builtins.int")
@specialize_function("builtins.float")
@specialize_function("builtins.complex")
@specialize_function("mypy_extensions.i64")
@specialize_function("mypy_extensions.i32")
@specialize_function("mypy_extensions.i16")
@specialize_function("mypy_extensions.u8")
def translate_builtins_with_unary_dunder(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr
) -> Value | None:
    """Specialize calls on native classes that implement the associated dunder.

    E.g. i64(x) gets specialized to x.__int__() if x is a native instance.
    """
    if len(expr.args) == 1 and expr.arg_kinds == [ARG_POS] and isinstance(callee, NameExpr):
        arg = expr.args[0]
        arg_typ = builder.node_type(arg)
        shortname = callee.fullname.split(".")[1]
        if shortname in ("i64", "i32", "i16", "u8"):
            method = "__int__"
        else:
            method = f"__{shortname}__"
        if isinstance(arg_typ, RInstance) and arg_typ.class_ir.has_method(method):
            obj = builder.accept(arg)
            return builder.gen_method_call(obj, method, [], None, expr.line)

    return None


@specialize_function("builtins.len")
def translate_len(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) == 1 and expr.arg_kinds == [ARG_POS]:
        arg = expr.args[0]
        expr_rtype = builder.node_type(arg)
        if isinstance(expr_rtype, RTuple):
            # len() of fixed-length tuple can be trivially determined
            # statically, though we still need to evaluate it.
            builder.accept(arg)
            return Integer(len(expr_rtype.types))
        else:
            if is_list_rprimitive(builder.node_type(arg)):
                borrow = True
            else:
                borrow = False
            obj = builder.accept(arg, can_borrow=borrow)
            return builder.builtin_len(obj, expr.line)
    return None


@specialize_function("builtins.list")
def dict_methods_fast_path(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    """Specialize a common case when list() is called on a dictionary
    view method call.

    For example:
        foo = list(bar.keys())
    """
    if not (len(expr.args) == 1 and expr.arg_kinds == [ARG_POS]):
        return None
    arg = expr.args[0]
    if not (isinstance(arg, CallExpr) and not arg.args and isinstance(arg.callee, MemberExpr)):
        return None
    base = arg.callee.expr
    attr = arg.callee.name
    rtype = builder.node_type(base)
    if not (is_dict_rprimitive(rtype) and attr in ("keys", "values", "items")):
        return None

    obj = builder.accept(base)
    # Note that it is not safe to use fast methods on dict subclasses,
    # so the corresponding helpers in CPy.h fallback to (inlined)
    # generic logic.
    if attr == "keys":
        return builder.call_c(dict_keys_op, [obj], expr.line)
    elif attr == "values":
        return builder.call_c(dict_values_op, [obj], expr.line)
    else:
        return builder.call_c(dict_items_op, [obj], expr.line)


@specialize_function("builtins.list")
def translate_list_from_generator_call(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr
) -> Value | None:
    """Special case for simplest list comprehension.

    For example:
        list(f(x) for x in some_list/some_tuple/some_str)
    'translate_list_comprehension()' would take care of other cases
    if this fails.
    """
    if (
        len(expr.args) == 1
        and expr.arg_kinds[0] == ARG_POS
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        return sequence_from_generator_preallocate_helper(
            builder,
            expr.args[0],
            empty_op_llbuilder=builder.builder.new_list_op_with_length,
            set_item_op=new_list_set_item_op,
        )
    return None


@specialize_function("builtins.tuple")
def translate_tuple_from_generator_call(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr
) -> Value | None:
    """Special case for simplest tuple creation from a generator.

    For example:
        tuple(f(x) for x in some_list/some_tuple/some_str)
    'translate_safe_generator_call()' would take care of other cases
    if this fails.
    """
    if (
        len(expr.args) == 1
        and expr.arg_kinds[0] == ARG_POS
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        return sequence_from_generator_preallocate_helper(
            builder,
            expr.args[0],
            empty_op_llbuilder=builder.builder.new_tuple_with_length,
            set_item_op=new_tuple_set_item_op,
        )
    return None


@specialize_function("builtins.set")
def translate_set_from_generator_call(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr
) -> Value | None:
    """Special case for set creation from a generator.

    For example:
        set(f(...) for ... in iterator/nested_generators...)
    """
    if (
        len(expr.args) == 1
        and expr.arg_kinds[0] == ARG_POS
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        return translate_set_comprehension(builder, expr.args[0])
    return None


@specialize_function("builtins.min")
@specialize_function("builtins.max")
def faster_min_max(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if expr.arg_kinds == [ARG_POS, ARG_POS]:
        x, y = builder.accept(expr.args[0]), builder.accept(expr.args[1])
        result = Register(builder.node_type(expr))
        # CPython evaluates arguments reversely when calling min(...) or max(...)
        if callee.fullname == "builtins.min":
            comparison = builder.binary_op(y, x, "<", expr.line)
        else:
            comparison = builder.binary_op(y, x, ">", expr.line)

        true_block, false_block, next_block = BasicBlock(), BasicBlock(), BasicBlock()
        builder.add_bool_branch(comparison, true_block, false_block)

        builder.activate_block(true_block)
        builder.assign(result, builder.coerce(y, result.type, expr.line), expr.line)
        builder.goto(next_block)

        builder.activate_block(false_block)
        builder.assign(result, builder.coerce(x, result.type, expr.line), expr.line)
        builder.goto(next_block)

        builder.activate_block(next_block)
        return result
    return None


@specialize_function("builtins.tuple")
@specialize_function("builtins.frozenset")
@specialize_function("builtins.dict")
@specialize_function("builtins.min")
@specialize_function("builtins.max")
@specialize_function("builtins.sorted")
@specialize_function("collections.OrderedDict")
@specialize_function("join", str_rprimitive)
@specialize_function("extend", list_rprimitive)
@specialize_function("update", dict_rprimitive)
@specialize_function("update", set_rprimitive)
def translate_safe_generator_call(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr
) -> Value | None:
    """Special cases for things that consume iterators where we know we
    can safely compile a generator into a list.
    """
    if (
        len(expr.args) > 0
        and expr.arg_kinds[0] == ARG_POS
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        if isinstance(callee, MemberExpr):
            return builder.gen_method_call(
                builder.accept(callee.expr),
                callee.name,
                (
                    [translate_list_comprehension(builder, expr.args[0])]
                    + [builder.accept(arg) for arg in expr.args[1:]]
                ),
                builder.node_type(expr),
                expr.line,
                expr.arg_kinds,
                expr.arg_names,
            )
        else:
            return builder.call_refexpr_with_args(
                expr,
                callee,
                (
                    [translate_list_comprehension(builder, expr.args[0])]
                    + [builder.accept(arg) for arg in expr.args[1:]]
                ),
            )
    return None


@specialize_function("builtins.any")
def translate_any_call(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if (
        len(expr.args) == 1
        and expr.arg_kinds == [ARG_POS]
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        return any_all_helper(builder, expr.args[0], builder.false, lambda x: x, builder.true)
    return None


@specialize_function("builtins.all")
def translate_all_call(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if (
        len(expr.args) == 1
        and expr.arg_kinds == [ARG_POS]
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        return any_all_helper(
            builder,
            expr.args[0],
            builder.true,
            lambda x: builder.unary_op(x, "not", expr.line),
            builder.false,
        )
    return None


def any_all_helper(
    builder: IRBuilder,
    gen: GeneratorExpr,
    initial_value: Callable[[], Value],
    modify: Callable[[Value], Value],
    new_value: Callable[[], Value],
) -> Value:
    retval = Register(bool_rprimitive)
    builder.assign(retval, initial_value(), -1)
    loop_params = list(zip(gen.indices, gen.sequences, gen.condlists, gen.is_async))
    true_block, false_block, exit_block = BasicBlock(), BasicBlock(), BasicBlock()

    def gen_inner_stmts() -> None:
        comparison = modify(builder.accept(gen.left_expr))
        builder.add_bool_branch(comparison, true_block, false_block)
        builder.activate_block(true_block)
        builder.assign(retval, new_value(), -1)
        builder.goto(exit_block)
        builder.activate_block(false_block)

    comprehension_helper(builder, loop_params, gen_inner_stmts, gen.line)
    builder.goto_and_activate(exit_block)

    return retval


@specialize_function("builtins.sum")
def translate_sum_call(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    # specialized implementation is used if:
    # - only one or two arguments given (if not, sum() has been given invalid arguments)
    # - first argument is a Generator (there is no benefit to optimizing the performance of eg.
    #   sum([1, 2, 3]), so non-Generator Iterables are not handled)
    if not (
        len(expr.args) in (1, 2)
        and expr.arg_kinds[0] == ARG_POS
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        return None

    # handle 'start' argument, if given
    if len(expr.args) == 2:
        # ensure call to sum() was properly constructed
        if expr.arg_kinds[1] not in (ARG_POS, ARG_NAMED):
            return None
        start_expr = expr.args[1]
    else:
        start_expr = IntExpr(0)

    gen_expr = expr.args[0]
    target_type = builder.node_type(expr)
    retval = Register(target_type)
    builder.assign(retval, builder.coerce(builder.accept(start_expr), target_type, -1), -1)

    def gen_inner_stmts() -> None:
        call_expr = builder.accept(gen_expr.left_expr)
        builder.assign(retval, builder.binary_op(retval, call_expr, "+", -1), -1)

    loop_params = list(
        zip(gen_expr.indices, gen_expr.sequences, gen_expr.condlists, gen_expr.is_async)
    )
    comprehension_helper(builder, loop_params, gen_inner_stmts, gen_expr.line)

    return retval


@specialize_function("dataclasses.field")
@specialize_function("attr.ib")
@specialize_function("attr.attrib")
@specialize_function("attr.Factory")
def translate_dataclasses_field_call(
    builder: IRBuilder, expr: CallExpr, callee: RefExpr
) -> Value | None:
    """Special case for 'dataclasses.field', 'attr.attrib', and 'attr.Factory'
    function calls because the results of such calls are type-checked
    by mypy using the types of the arguments to their respective
    functions, resulting in attempted coercions by mypyc that throw a
    runtime error.
    """
    builder.types[expr] = AnyType(TypeOfAny.from_error)
    return None


@specialize_function("builtins.next")
def translate_next_call(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    """Special case for calling next() on a generator expression, an
    idiom that shows up some in mypy.

    For example, next(x for x in l if x.id == 12, None) will
    generate code that searches l for an element where x.id == 12
    and produce the first such object, or None if no such element
    exists.
    """
    if not (
        expr.arg_kinds in ([ARG_POS], [ARG_POS, ARG_POS])
        and isinstance(expr.args[0], GeneratorExpr)
    ):
        return None

    gen = expr.args[0]
    retval = Register(builder.node_type(expr))
    default_val = builder.accept(expr.args[1]) if len(expr.args) > 1 else None
    exit_block = BasicBlock()

    def gen_inner_stmts() -> None:
        # next takes the first element of the generator, so if
        # something gets produced, we are done.
        builder.assign(retval, builder.accept(gen.left_expr), gen.left_expr.line)
        builder.goto(exit_block)

    loop_params = list(zip(gen.indices, gen.sequences, gen.condlists, gen.is_async))
    comprehension_helper(builder, loop_params, gen_inner_stmts, gen.line)

    # Now we need the case for when nothing got hit. If there was
    # a default value, we produce it, and otherwise we raise
    # StopIteration.
    if default_val:
        builder.assign(retval, default_val, gen.left_expr.line)
        builder.goto(exit_block)
    else:
        builder.add(RaiseStandardError(RaiseStandardError.STOP_ITERATION, None, expr.line))
        builder.add(Unreachable())

    builder.activate_block(exit_block)
    return retval


isinstance_primitives: Final = {
    "builtins.bool": isinstance_bool,
    "builtins.bytearray": isinstance_bytearray,
    "builtins.bytes": isinstance_bytes,
    "builtins.dict": isinstance_dict,
    "builtins.float": isinstance_float,
    "builtins.frozenset": isinstance_frozenset,
    "builtins.int": isinstance_int,
    "builtins.list": isinstance_list,
    "builtins.set": isinstance_set,
    "builtins.str": isinstance_str,
    "builtins.tuple": isinstance_tuple,
}


@specialize_function("builtins.isinstance")
def translate_isinstance(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    """Special case for builtins.isinstance.

    Prevent coercions on the thing we are checking the instance of -
    there is no need to coerce something to a new type before checking
    what type it is, and the coercion could lead to bugs.
    """
    if not (len(expr.args) == 2 and expr.arg_kinds == [ARG_POS, ARG_POS]):
        return None

    if isinstance(expr.args[1], (RefExpr, TupleExpr)):
        builder.types[expr.args[0]] = AnyType(TypeOfAny.from_error)

        irs = builder.flatten_classes(expr.args[1])
        if irs is not None:
            can_borrow = all(
                ir.is_ext_class and not ir.inherits_python and not ir.allow_interpreted_subclasses
                for ir in irs
            )
            obj = builder.accept(expr.args[0], can_borrow=can_borrow)
            return builder.builder.isinstance_helper(obj, irs, expr.line)

    if isinstance(expr.args[1], RefExpr):
        node = expr.args[1].node
        if node:
            desc = isinstance_primitives.get(node.fullname)
            if desc:
                obj = builder.accept(expr.args[0])
                return builder.primitive_op(desc, [obj], expr.line)

    return None


@specialize_function("setdefault", dict_rprimitive)
def translate_dict_setdefault(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    """Special case for 'dict.setdefault' which would only construct
    default empty collection when needed.

    The dict_setdefault_spec_init_op checks whether the dict contains
    the key and would construct the empty collection only once.

    For example, this specializer works for the following cases:
         d.setdefault(key, set()).add(value)
         d.setdefault(key, []).append(value)
         d.setdefault(key, {})[inner_key] = inner_val
    """
    if (
        len(expr.args) == 2
        and expr.arg_kinds == [ARG_POS, ARG_POS]
        and isinstance(callee, MemberExpr)
    ):
        arg = expr.args[1]
        if isinstance(arg, ListExpr):
            if len(arg.items):
                return None
            data_type = Integer(1, c_int_rprimitive, expr.line)
        elif isinstance(arg, DictExpr):
            if len(arg.items):
                return None
            data_type = Integer(2, c_int_rprimitive, expr.line)
        elif (
            isinstance(arg, CallExpr)
            and isinstance(arg.callee, NameExpr)
            and arg.callee.fullname == "builtins.set"
        ):
            if len(arg.args):
                return None
            data_type = Integer(3, c_int_rprimitive, expr.line)
        else:
            return None

        callee_dict = builder.accept(callee.expr)
        key_val = builder.accept(expr.args[0])
        return builder.call_c(
            dict_setdefault_spec_init_op, [callee_dict, key_val, data_type], expr.line
        )
    return None


@specialize_function("format", str_rprimitive)
def translate_str_format(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if (
        isinstance(callee, MemberExpr)
        and isinstance(callee.expr, StrExpr)
        and expr.arg_kinds.count(ARG_POS) == len(expr.arg_kinds)
    ):
        format_str = callee.expr.value
        tokens = tokenizer_format_call(format_str)
        if tokens is None:
            return None
        literals, format_ops = tokens
        # Convert variables to strings
        substitutions = convert_format_expr_to_str(builder, format_ops, expr.args, expr.line)
        if substitutions is None:
            return None
        return join_formatted_strings(builder, literals, substitutions, expr.line)
    return None


@specialize_function("join", str_rprimitive)
def translate_fstring(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    """Special case for f-string, which is translated into str.join()
    in mypy AST.

    This specializer optimizes simplest f-strings which don't contain
    any format operation.
    """
    if (
        isinstance(callee, MemberExpr)
        and isinstance(callee.expr, StrExpr)
        and callee.expr.value == ""
        and expr.arg_kinds == [ARG_POS]
        and isinstance(expr.args[0], ListExpr)
    ):
        for item in expr.args[0].items:
            if isinstance(item, StrExpr):
                continue
            elif isinstance(item, CallExpr):
                if not isinstance(item.callee, MemberExpr) or item.callee.name != "format":
                    return None
                elif (
                    not isinstance(item.callee.expr, StrExpr) or item.callee.expr.value != "{:{}}"
                ):
                    return None

                if not isinstance(item.args[1], StrExpr) or item.args[1].value != "":
                    return None
            else:
                return None

        format_ops = []
        exprs: list[Expression] = []

        for item in expr.args[0].items:
            if isinstance(item, StrExpr) and item.value != "":
                format_ops.append(FormatOp.STR)
                exprs.append(item)
            elif isinstance(item, CallExpr):
                format_ops.append(FormatOp.STR)
                exprs.append(item.args[0])

        substitutions = convert_format_expr_to_str(builder, format_ops, exprs, expr.line)
        if substitutions is None:
            return None

        return join_formatted_strings(builder, None, substitutions, expr.line)
    return None


@specialize_function("encode", str_rprimitive)
def str_encode_fast_path(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    """Specialize common cases of str.encode for most used encodings and strict errors."""

    if not isinstance(callee, MemberExpr):
        return None

    # We can only specialize if we have string literals as args
    if len(expr.arg_kinds) > 0 and not isinstance(expr.args[0], StrExpr):
        return None
    if len(expr.arg_kinds) > 1 and not isinstance(expr.args[1], StrExpr):
        return None

    encoding = "utf8"
    errors = "strict"
    if len(expr.arg_kinds) > 0 and isinstance(expr.args[0], StrExpr):
        if expr.arg_kinds[0] == ARG_NAMED:
            if expr.arg_names[0] == "encoding":
                encoding = expr.args[0].value
            elif expr.arg_names[0] == "errors":
                errors = expr.args[0].value
        elif expr.arg_kinds[0] == ARG_POS:
            encoding = expr.args[0].value
        else:
            return None
    if len(expr.arg_kinds) > 1 and isinstance(expr.args[1], StrExpr):
        if expr.arg_kinds[1] == ARG_NAMED:
            if expr.arg_names[1] == "encoding":
                encoding = expr.args[1].value
            elif expr.arg_names[1] == "errors":
                errors = expr.args[1].value
        elif expr.arg_kinds[1] == ARG_POS:
            errors = expr.args[1].value
        else:
            return None

    if errors != "strict":
        # We can only specialize strict errors
        return None

    encoding = encoding.lower().replace("-", "").replace("_", "")  # normalize
    # Specialized encodings and their accepted aliases
    if encoding in ["u8", "utf", "utf8", "cp65001"]:
        return builder.call_c(str_encode_utf8_strict, [builder.accept(callee.expr)], expr.line)
    elif encoding in ["646", "ascii", "usascii"]:
        return builder.call_c(str_encode_ascii_strict, [builder.accept(callee.expr)], expr.line)
    elif encoding in ["iso88591", "8859", "cp819", "latin", "latin1", "l1"]:
        return builder.call_c(str_encode_latin1_strict, [builder.accept(callee.expr)], expr.line)

    return None


@specialize_function("mypy_extensions.i64")
def translate_i64(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    arg_type = builder.node_type(arg)
    if is_int64_rprimitive(arg_type):
        return builder.accept(arg)
    elif is_int32_rprimitive(arg_type) or is_int16_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.add(Extend(val, int64_rprimitive, signed=True, line=expr.line))
    elif is_uint8_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.add(Extend(val, int64_rprimitive, signed=False, line=expr.line))
    elif is_int_rprimitive(arg_type) or is_bool_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.coerce(val, int64_rprimitive, expr.line)
    return None


@specialize_function("mypy_extensions.i32")
def translate_i32(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    arg_type = builder.node_type(arg)
    if is_int32_rprimitive(arg_type):
        return builder.accept(arg)
    elif is_int64_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.add(Truncate(val, int32_rprimitive, line=expr.line))
    elif is_int16_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.add(Extend(val, int32_rprimitive, signed=True, line=expr.line))
    elif is_uint8_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.add(Extend(val, int32_rprimitive, signed=False, line=expr.line))
    elif is_int_rprimitive(arg_type) or is_bool_rprimitive(arg_type):
        val = builder.accept(arg)
        val = truncate_literal(val, int32_rprimitive)
        return builder.coerce(val, int32_rprimitive, expr.line)
    return None


@specialize_function("mypy_extensions.i16")
def translate_i16(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    arg_type = builder.node_type(arg)
    if is_int16_rprimitive(arg_type):
        return builder.accept(arg)
    elif is_int32_rprimitive(arg_type) or is_int64_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.add(Truncate(val, int16_rprimitive, line=expr.line))
    elif is_uint8_rprimitive(arg_type):
        val = builder.accept(arg)
        return builder.add(Extend(val, int16_rprimitive, signed=False, line=expr.line))
    elif is_int_rprimitive(arg_type) or is_bool_rprimitive(arg_type):
        val = builder.accept(arg)
        val = truncate_literal(val, int16_rprimitive)
        return builder.coerce(val, int16_rprimitive, expr.line)
    return None


@specialize_function("mypy_extensions.u8")
def translate_u8(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    arg_type = builder.node_type(arg)
    if is_uint8_rprimitive(arg_type):
        return builder.accept(arg)
    elif (
        is_int16_rprimitive(arg_type)
        or is_int32_rprimitive(arg_type)
        or is_int64_rprimitive(arg_type)
    ):
        val = builder.accept(arg)
        return builder.add(Truncate(val, uint8_rprimitive, line=expr.line))
    elif is_int_rprimitive(arg_type) or is_bool_rprimitive(arg_type):
        val = builder.accept(arg)
        val = truncate_literal(val, uint8_rprimitive)
        return builder.coerce(val, uint8_rprimitive, expr.line)
    return None


def truncate_literal(value: Value, rtype: RPrimitive) -> Value:
    """If value is an integer literal value, truncate it to given native int rtype.

    For example, truncate 256 into 0 if rtype is u8.
    """
    if not isinstance(value, Integer):
        return value  # Not a literal, nothing to do
    x = value.numeric_value()
    max_unsigned = (1 << (rtype.size * 8)) - 1
    x = x & max_unsigned
    if rtype.is_signed and x >= (max_unsigned + 1) // 2:
        # Adjust to make it a negative value
        x -= max_unsigned + 1
    return Integer(x, rtype)


@specialize_function("builtins.int")
def translate_int(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    arg_type = builder.node_type(arg)
    if (
        is_bool_rprimitive(arg_type)
        or is_int_rprimitive(arg_type)
        or is_fixed_width_rtype(arg_type)
    ):
        src = builder.accept(arg)
        return builder.coerce(src, int_rprimitive, expr.line)
    return None


@specialize_function("builtins.bool")
def translate_bool(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    src = builder.accept(arg)
    return builder.builder.bool_value(src)


@specialize_function("builtins.float")
def translate_float(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    arg_type = builder.node_type(arg)
    if is_float_rprimitive(arg_type):
        # No-op float conversion.
        return builder.accept(arg)
    return None


@specialize_function("builtins.ord")
def translate_ord(builder: IRBuilder, expr: CallExpr, callee: RefExpr) -> Value | None:
    if len(expr.args) != 1 or expr.arg_kinds[0] != ARG_POS:
        return None
    arg = expr.args[0]
    if isinstance(arg, (StrExpr, BytesExpr)) and len(arg.value) == 1:
        return Integer(ord(arg.value))
    return None
