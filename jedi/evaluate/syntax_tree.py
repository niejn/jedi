"""
Functions evaluating the syntax tree.
"""
import copy
import operator as op

from parso.python import tree

from jedi import debug
from jedi import parser_utils
from jedi.evaluate.context import ContextSet, NO_CONTEXTS, ContextualizedNode, \
    ContextualizedName, iterator_to_context_set
from jedi.evaluate import compiled
from jedi.evaluate import pep0484
from jedi.evaluate import recursion
from jedi.evaluate import helpers
from jedi.evaluate import analysis


def _limit_context_infers(func):
    """
    This is for now the way how we limit type inference going wild. There are
    other ways to ensure recursion limits as well. This is mostly necessary
    because of instance (self) access that can be quite tricky to limit.

    I'm still not sure this is the way to go, but it looks okay for now and we
    can still go anther way in the future. Tests are there. ~ dave
    """
    def wrapper(context, *args, **kwargs):
        n = context.tree_node
        evaluator = context.evaluator
        try:
            evaluator.inferred_element_counts[n] += 1
            if evaluator.inferred_element_counts[n] > 300:
                debug.warning('In context %s there were too many inferences.', n)
                return NO_CONTEXTS
        except KeyError:
            evaluator.inferred_element_counts[n] = 1
        return func(context, *args, **kwargs)

    return wrapper


@debug.increase_indent
@_limit_context_infers
def eval_node(context, element):
    debug.dbg('eval_element %s@%s', element, element.start_pos)
    evaluator = context.evaluator
    typ = element.type
    if typ in ('name', 'number', 'string', 'atom'):
        return eval_atom(context, element)
    elif typ == 'keyword':
        # For False/True/None
        if element.value in ('False', 'True', 'None'):
            return ContextSet(compiled.builtin_from_name(evaluator, element.value))
        # else: print e.g. could be evaluated like this in Python 2.7
        return NO_CONTEXTS
    elif typ == 'lambdef':
        from jedi.evaluate import representation as er
        return ContextSet(er.FunctionContext(evaluator, context, element))
    elif typ == 'expr_stmt':
        return eval_expr_stmt(context, element)
    elif typ in ('power', 'atom_expr'):
        first_child = element.children[0]
        if not (first_child.type == 'keyword' and first_child.value == 'await'):
            context_set = eval_atom(context, first_child)
            for trailer in element.children[1:]:
                if trailer == '**':  # has a power operation.
                    right = evaluator.eval_element(context, element.children[2])
                    context_set = _eval_comparison(
                        evaluator,
                        context,
                        context_set,
                        trailer,
                        right
                    )
                    break
                context_set = eval_trailer(context, context_set, trailer)
            return context_set
        return NO_CONTEXTS
    elif typ in ('testlist_star_expr', 'testlist',):
        # The implicit tuple in statements.
        from jedi.evaluate import iterable
        return ContextSet(iterable.SequenceLiteralContext(evaluator, context, element))
    elif typ in ('not_test', 'factor'):
        context_set = context.eval_node(element.children[-1])
        for operator in element.children[:-1]:
            context_set = eval_factor(context_set, operator)
        return context_set
    elif typ == 'test':
        # `x if foo else y` case.
        return (context.eval_node(element.children[0]) |
                context.eval_node(element.children[-1]))
    elif typ == 'operator':
        # Must be an ellipsis, other operators are not evaluated.
        # In Python 2 ellipsis is coded as three single dot tokens, not
        # as one token 3 dot token.
        assert element.value in ('.', '...')
        return ContextSet(compiled.create(evaluator, Ellipsis))
    elif typ == 'dotted_name':
        context_set = eval_atom(context, element.children[0])
        for next_name in element.children[2::2]:
            # TODO add search_global=True?
            context_set = context_set.py__getattribute__(next_name, name_context=context)
        return context_set
    elif typ == 'eval_input':
        return eval_node(context, element.children[0])
    elif typ == 'annassign':
        return pep0484._evaluate_for_annotation(context, element.children[1])
    else:
        return eval_or_test(context, element)


def eval_trailer(context, base_contexts, trailer):
    trailer_op, node = trailer.children[:2]
    if node == ')':  # `arglist` is optional.
        node = ()

    if trailer_op == '[':
        from jedi.evaluate import iterable
        return iterable.py__getitem__(context.evaluator, context, base_contexts, trailer)
    else:
        debug.dbg('eval_trailer: %s in %s', trailer, base_contexts)
        if trailer_op == '.':
            return base_contexts.py__getattribute__(
                name_context=context,
                name_or_str=node
            )
        else:
            assert trailer_op == '('
            from jedi.evaluate import param
            arguments = param.TreeArguments(context.evaluator, context, node, trailer)
            return base_contexts.execute(arguments)


def eval_atom(context, atom):
    """
    Basically to process ``atom`` nodes. The parser sometimes doesn't
    generate the node (because it has just one child). In that case an atom
    might be a name or a literal as well.
    """
    from jedi.evaluate import iterable
    if atom.type == 'name':
        # This is the first global lookup.
        stmt = tree.search_ancestor(
            atom, 'expr_stmt', 'lambdef'
        ) or atom
        if stmt.type == 'lambdef':
            stmt = atom
        return context.py__getattribute__(
            name_or_str=atom,
            position=stmt.start_pos,
            search_global=True
        )

    elif isinstance(atom, tree.Literal):
        string = parser_utils.safe_literal_eval(atom.value)
        return ContextSet(compiled.create(context.evaluator, string))
    else:
        c = atom.children
        if c[0].type == 'string':
            # Will be one string.
            context_set = eval_atom(context, c[0])
            for string in c[1:]:
                right = eval_atom(context, string)
                context_set = _eval_comparison(context.evaluator, context, context_set, '+', right)
            return context_set
        # Parentheses without commas are not tuples.
        elif c[0] == '(' and not len(c) == 2 \
                and not(c[1].type == 'testlist_comp' and
                        len(c[1].children) > 1):
            return context.eval_node(c[1])

        try:
            comp_for = c[1].children[1]
        except (IndexError, AttributeError):
            pass
        else:
            if comp_for == ':':
                # Dict comprehensions have a colon at the 3rd index.
                try:
                    comp_for = c[1].children[3]
                except IndexError:
                    pass

            if comp_for.type == 'comp_for':
                return ContextSet(iterable.Comprehension.from_atom(context.evaluator, context, atom))

        # It's a dict/list/tuple literal.
        array_node = c[1]
        try:
            array_node_c = array_node.children
        except AttributeError:
            array_node_c = []
        if c[0] == '{' and (array_node == '}' or ':' in array_node_c):
            context = iterable.DictLiteralContext(context.evaluator, context, atom)
        else:
            context = iterable.SequenceLiteralContext(context.evaluator, context, atom)
        return ContextSet(context)


@_limit_context_infers
def eval_expr_stmt(context, stmt, seek_name=None):
    with recursion.execution_allowed(context.evaluator, stmt) as allowed:
        if allowed or context.get_root_context() == context.evaluator.BUILTINS:
            return _eval_expr_stmt(context, stmt, seek_name)
    return NO_CONTEXTS


@debug.increase_indent
def _eval_expr_stmt(context, stmt, seek_name=None):
    """
    The starting point of the completion. A statement always owns a call
    list, which are the calls, that a statement does. In case multiple
    names are defined in the statement, `seek_name` returns the result for
    this name.

    :param stmt: A `tree.ExprStmt`.
    """
    debug.dbg('eval_expr_stmt %s (%s)', stmt, seek_name)
    rhs = stmt.get_rhs()
    context_set = context.eval_node(rhs)

    if seek_name:
        c_node = ContextualizedName(context, seek_name)
        from jedi.evaluate import finder
        context_set = finder.check_tuple_assignments(context.evaluator, c_node, context_set)

    first_operator = next(stmt.yield_operators(), None)
    if first_operator not in ('=', None) and first_operator.type == 'operator':
        # `=` is always the last character in aug assignments -> -1
        operator = copy.copy(first_operator)
        operator.value = operator.value[:-1]
        name = stmt.get_defined_names()[0].value
        left = context.py__getattribute__(
            name, position=stmt.start_pos, search_global=True)

        for_stmt = tree.search_ancestor(stmt, 'for_stmt')
        if for_stmt is not None and for_stmt.type == 'for_stmt' and context_set \
                and parser_utils.for_stmt_defines_one_name(for_stmt):
            # Iterate through result and add the values, that's possible
            # only in for loops without clutter, because they are
            # predictable. Also only do it, if the variable is not a tuple.
            node = for_stmt.get_testlist()
            cn = ContextualizedNode(context, node)
            from jedi.evaluate import iterable
            ordered = list(iterable.py__iter__(context.evaluator, cn.infer(), cn))

            for lazy_context in ordered:
                dct = {for_stmt.children[1].value: lazy_context.infer()}
                with helpers.predefine_names(context, for_stmt, dct):
                    t = context.eval_node(rhs)
                    left = _eval_comparison(context.evaluator, context, left, operator, t)
            context_set = left
        else:
            context_set = _eval_comparison(context.evaluator, context, left, operator, context_set)
    debug.dbg('eval_expr_stmt result %s', context_set)
    return context_set


def eval_or_test(context, or_test):
    iterator = iter(or_test.children)
    types = context.eval_node(next(iterator))
    for operator in iterator:
        right = next(iterator)
        if operator.type == 'comp_op':  # not in / is not
            operator = ' '.join(c.value for c in operator.children)

        # handle lazy evaluation of and/or here.
        if operator in ('and', 'or'):
            left_bools = set(left.py__bool__() for left in types)
            if left_bools == set([True]):
                if operator == 'and':
                    types = context.eval_node(right)
            elif left_bools == set([False]):
                if operator != 'and':
                    types = context.eval_node(right)
            # Otherwise continue, because of uncertainty.
        else:
            types = _eval_comparison(context.evaluator, context, types, operator,
                                         context.eval_node(right))
    debug.dbg('eval_or_test types %s', types)
    return types


@iterator_to_context_set
def eval_factor(context_set, operator):
    """
    Calculates `+`, `-`, `~` and `not` prefixes.
    """
    for context in context_set:
        if operator == '-':
            if _is_number(context):
                yield compiled.create(context.evaluator, -context.obj)
        elif operator == 'not':
            value = context.py__bool__()
            if value is None:  # Uncertainty.
                return
            yield compiled.create(context.evaluator, not value)
        else:
            yield context


# Maps Python syntax to the operator module.
COMPARISON_OPERATORS = {
    '==': op.eq,
    '!=': op.ne,
    'is': op.is_,
    'is not': op.is_not,
    '<': op.lt,
    '<=': op.le,
    '>': op.gt,
    '>=': op.ge,
}


def _literals_to_types(evaluator, result):
    # Changes literals ('a', 1, 1.0, etc) to its type instances (str(),
    # int(), float(), etc).
    new_result = NO_CONTEXTS
    for typ in result:
        if _is_literal(typ):
            # Literals are only valid as long as the operations are
            # correct. Otherwise add a value-free instance.
            cls = compiled.builtin_from_name(evaluator, typ.name.string_name)
            new_result |= cls.execute_evaluated()
        else:
            new_result |= ContextSet(typ)
    return new_result


def _eval_comparison(evaluator, context, left_contexts, operator, right_contexts):
    if not left_contexts or not right_contexts:
        # illegal slices e.g. cause left/right_result to be None
        result = (left_contexts or NO_CONTEXTS) | (right_contexts or NO_CONTEXTS)
        return _literals_to_types(evaluator, result)
    else:
        # I don't think there's a reasonable chance that a string
        # operation is still correct, once we pass something like six
        # objects.
        if len(left_contexts) * len(right_contexts) > 6:
            return _literals_to_types(evaluator, left_contexts | right_contexts)
        else:
            return ContextSet.from_sets(
                _eval_comparison_part(evaluator, context, left, operator, right)
                for left in left_contexts
                for right in right_contexts
            )


def _is_compiled(context):
    return isinstance(context, compiled.CompiledObject)


def _is_number(context):
    return _is_compiled(context) and isinstance(context.obj, (int, float))


def is_string(context):
    return _is_compiled(context) and isinstance(context.obj, (str, unicode))


def _is_literal(context):
    return _is_number(context) or is_string(context)


def _is_tuple(context):
    from jedi.evaluate import iterable
    return isinstance(context, iterable.AbstractSequence) and context.array_type == 'tuple'


def _is_list(context):
    from jedi.evaluate import iterable
    return isinstance(context, iterable.AbstractSequence) and context.array_type == 'list'


def _eval_comparison_part(evaluator, context, left, operator, right):
    from jedi.evaluate import iterable, instance
    l_is_num = _is_number(left)
    r_is_num = _is_number(right)
    if operator == '*':
        # for iterables, ignore * operations
        if isinstance(left, iterable.AbstractSequence) or is_string(left):
            return ContextSet(left)
        elif isinstance(right, iterable.AbstractSequence) or is_string(right):
            return ContextSet(right)
    elif operator == '+':
        if l_is_num and r_is_num or is_string(left) and is_string(right):
            return ContextSet(compiled.create(evaluator, left.obj + right.obj))
        elif _is_tuple(left) and _is_tuple(right) or _is_list(left) and _is_list(right):
            return ContextSet(iterable.MergedArray(evaluator, (left, right)))
    elif operator == '-':
        if l_is_num and r_is_num:
            return ContextSet(compiled.create(evaluator, left.obj - right.obj))
    elif operator == '%':
        # With strings and numbers the left type typically remains. Except for
        # `int() % float()`.
        return ContextSet(left)
    elif operator in COMPARISON_OPERATORS:
        operation = COMPARISON_OPERATORS[operator]
        if _is_compiled(left) and _is_compiled(right):
            # Possible, because the return is not an option. Just compare.
            left = left.obj
            right = right.obj

        try:
            result = operation(left, right)
        except TypeError:
            # Could be True or False.
            return ContextSet(compiled.create(evaluator, True), compiled.create(evaluator, False))
        else:
            return ContextSet(compiled.create(evaluator, result))
    elif operator == 'in':
        return NO_CONTEXTS

    def check(obj):
        """Checks if a Jedi object is either a float or an int."""
        return isinstance(obj, instance.CompiledInstance) and \
            obj.name.string_name in ('int', 'float')

    # Static analysis, one is a number, the other one is not.
    if operator in ('+', '-') and l_is_num != r_is_num \
            and not (check(left) or check(right)):
        message = "TypeError: unsupported operand type(s) for +: %s and %s"
        analysis.add(context, 'type-error-operation', operator,
                     message % (left, right))

    return ContextSet(left, right)
