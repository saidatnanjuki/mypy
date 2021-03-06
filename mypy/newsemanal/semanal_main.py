"""Top-level logic for the new semantic analyzer.

The semantic analyzer binds names, resolves imports, detects various
special constructs that don't have dedicated AST nodes after parse
(such as 'cast' which looks like a call), and performs various simple
consistency checks.

Semantic analysis of each SCC (strongly connected component; import
cycle) is performed in one unit. Each module is analyzed as multiple
separate *targets*; the module top level is one target and each function
is a target. Nested functions are not separate targets, however. This is
mostly identical to targets used by mypy daemon (but classes aren't
targets in semantic analysis).

We first analyze each module top level in an SCC. If we encounter some
names that we can't bind because the target of the name may not have
been processed yet, we *defer* the current target for further
processing. Deferred targets will be analyzed additional times until
everything can be bound, or we reach a maximum number of iterations.

We keep track of a set of incomplete namespaces, i.e. namespaces that we
haven't finished populating yet. References to these namespaces cause a
deferral if they can't be satisfied. Initially every module in the SCC
will be incomplete.
"""

from typing import List, Tuple, Optional, Union

from mypy.nodes import (
    Node, SymbolTable, SymbolNode, MypyFile, TypeInfo, FuncDef, Decorator, OverloadedFuncDef
)
from mypy.newsemanal.semanal_typeargs import TypeArgumentAnalyzer
from mypy.state import strict_optional_set
from mypy.newsemanal.semanal import NewSemanticAnalyzer
from mypy.newsemanal.semanal_abstract import calculate_abstract_status
from mypy.errors import Errors
from mypy.newsemanal.semanal_infer import infer_decorator_signature_if_simple

MYPY = False
if MYPY:
    from mypy.build import Graph, State


# Perform up to this many semantic analysis iterations until giving up trying to bind all names.
MAX_ITERATIONS = 10

# Number of passes over core modules before going on to the rest of the builtin SCC.
CORE_WARMUP = 2
core_modules = ['typing', 'builtins', 'abc', 'collections']


def semantic_analysis_for_scc(graph: 'Graph', scc: List[str], errors: Errors) -> None:
    """Perform semantic analysis for all modules in a SCC (import cycle).

    Assume that reachability analysis has already been performed.
    """
    # Note that functions can't define new module-level attributes
    # using 'global x', since module top levels are fully processed
    # before functions. This limitation is unlikely to go away soon.
    process_top_levels(graph, scc)
    process_functions(graph, scc)
    check_type_arguments(graph, scc, errors)
    process_abstract_status(graph, scc, errors)


def process_top_levels(graph: 'Graph', scc: List[str]) -> None:
    # Process top levels until everything has been bound.

    # Initialize ASTs and symbol tables.
    for id in scc:
        state = graph[id]
        assert state.tree is not None
        state.manager.new_semantic_analyzer.prepare_file(state.tree)

    # Initially all namespaces in the SCC are incomplete (well they are empty).
    state.manager.incomplete_namespaces.update(scc)

    worklist = scc[:]
    # HACK: process core stuff first. This is mostly needed to support defining
    # named tuples in builtin SCC.
    if all(m in worklist for m in core_modules):
        worklist += list(reversed(core_modules)) * CORE_WARMUP
    iteration = 0
    final_iteration = False
    while worklist:
        iteration += 1
        if iteration == MAX_ITERATIONS:
            # Give up. Likely it's impossible to bind all names.
            state.manager.incomplete_namespaces.clear()
            final_iteration = True
        elif iteration > MAX_ITERATIONS:
            assert False, 'Max iteration count reached in semantic analysis'
        all_deferred = []  # type: List[str]
        while worklist:
            next_id = worklist.pop()
            state = graph[next_id]
            assert state.tree is not None
            deferred, incomplete = semantic_analyze_target(next_id, state, state.tree, None,
                                                           final_iteration)
            all_deferred += deferred
            if not incomplete:
                state.manager.incomplete_namespaces.discard(next_id)
        # Reverse to process the targets in the same order on every iteration. This avoids
        # processing the same target twice in a row, which is inefficient.
        worklist = list(reversed(all_deferred))


def process_functions(graph: 'Graph', scc: List[str]) -> None:
    # Process functions.
    for module in scc:
        tree = graph[module].tree
        assert tree is not None
        analyzer = graph[module].manager.new_semantic_analyzer
        symtable = tree.names
        targets = get_all_leaf_targets(symtable, module, None)
        for target, node, active_type in targets:
            assert isinstance(node, (FuncDef, OverloadedFuncDef, Decorator))
            process_top_level_function(analyzer, graph[module], module, target, node, active_type)


def process_top_level_function(analyzer: 'NewSemanticAnalyzer',
                               state: 'State',
                               module: str,
                               target: str,
                               node: Union[FuncDef, OverloadedFuncDef, Decorator],
                               active_type: Optional[TypeInfo]) -> None:
    """Analyze single top-level function or method.

    Process the body of the function (including nested functions) again and again,
    until all names have been resolved (ot iteration limit reached).
    """
    iteration = 0
    # We need one more iteration after incomplete is False (e.g. to report errors, if any).
    more_iterations = incomplete = True
    # Start in the incomplete state (no missing names will be reported on first pass).
    # Note that we use module name, since functions don't create qualified names.
    deferred = [module]
    analyzer.incomplete_namespaces.add(module)
    while deferred and more_iterations:
        iteration += 1
        if not (deferred or incomplete) or iteration == MAX_ITERATIONS:
            # OK, this is one last pass, now missing names will be reported.
            more_iterations = False
            analyzer.incomplete_namespaces.discard(module)
        deferred, incomplete = semantic_analyze_target(target, state, node, active_type,
                                                       not more_iterations)

    analyzer.incomplete_namespaces.discard(module)
    # After semantic analysis is done, discard local namespaces
    # to avoid memory hoarding.
    analyzer.saved_locals.clear()


TargetInfo = Tuple[str, Union[MypyFile, FuncDef, OverloadedFuncDef, Decorator], Optional[TypeInfo]]


def get_all_leaf_targets(symtable: SymbolTable,
                         prefix: str,
                         active_type: Optional[TypeInfo]) -> List[TargetInfo]:
    """Return all leaf targets in a symbol table (module-level and methods)."""
    result = []  # type: List[TargetInfo]
    for name, node in symtable.items():
        shortname = name
        if '-redef' in name:
            # Restore original name from mangled name of multiply defined function
            shortname = name.split('-redef')[0]
        new_prefix = prefix + '.' + shortname
        if isinstance(node.node, (FuncDef, TypeInfo, OverloadedFuncDef, Decorator)):
            if node.node.fullname() == new_prefix:
                if isinstance(node.node, TypeInfo):
                    result += get_all_leaf_targets(node.node.names, new_prefix, node.node)
                else:
                    result.append((new_prefix, node.node, active_type))
    return result


def semantic_analyze_target(target: str,
                            state: 'State',
                            node: Union[MypyFile, FuncDef, OverloadedFuncDef, Decorator],
                            active_type: Optional[TypeInfo],
                            final_iteration: bool) -> Tuple[List[str], bool]:
    tree = state.tree
    assert tree is not None
    analyzer = state.manager.new_semantic_analyzer
    # TODO: Move initialization to somewhere else
    analyzer.global_decls = [set()]
    analyzer.nonlocal_decls = [set()]
    analyzer.globals = tree.names
    with state.wrap_context():
        with analyzer.file_context(file_node=tree,
                                   fnam=tree.path,
                                   options=state.options,
                                   active_type=active_type):
            refresh_node = node
            if isinstance(refresh_node, Decorator):
                # Decorator expressions will be processed as part of the module top level.
                refresh_node = refresh_node.func
            analyzer.refresh_partial(refresh_node, [], final_iteration)
            if isinstance(node, Decorator):
                infer_decorator_signature_if_simple(node, analyzer)
    if analyzer.deferred:
        return [target], analyzer.incomplete
    else:
        return [], analyzer.incomplete


def check_type_arguments(graph: 'Graph', scc: List[str], errors: Errors) -> None:
    for module in scc:
        state = graph[module]
        assert state.tree
        analyzer = TypeArgumentAnalyzer(errors)
        with state.wrap_context():
            with strict_optional_set(state.options.strict_optional):
                state.tree.accept(analyzer)


def process_abstract_status(graph: 'Graph', scc: List[str], errors: Errors) -> None:
    for module in scc:
        tree = graph[module].tree
        assert tree
        calculate_abstract_status(tree, errors)
