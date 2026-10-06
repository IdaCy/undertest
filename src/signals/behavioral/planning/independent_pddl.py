"""Independent finite positive STRIPS parsing and supplied-plan replay.

Both domain and problem facts are derived from raw PDDL. No planner, accepted
grader, generated plan, or parsed-state import is used. Deletes precede adds.
"""
from dataclasses import dataclass
import hashlib
import json
import re


class PDDLError(ValueError):
    pass


class ActionDocumentError(ValueError):
    pass


def ensure(condition, detail):
    if not condition:
        raise PDDLError(detail)


def identifier(value, variable=False):
    pattern = r'\?[a-z_][a-z0-9_-]*' if variable else r'[a-z_][a-z0-9_-]*'
    ensure(isinstance(value, str) and re.fullmatch(pattern, value) is not None,
           'invalid_identifier:' + repr(value))
    return value


def parse_parentheses(raw):
    """Recursive token reader, consuming exactly one balanced document."""
    try:
        text = raw.decode('ascii').lower()
    except UnicodeDecodeError as exc:
        raise PDDLError('non_ascii_pddl') from exc
    tokens = iter(re.findall(r'\(|\)|[^\s()]+', re.sub(r';[^\n]*', '', text)))
    def read(token):
        ensure(token == '(', 'expected_open_parenthesis')
        children = []
        for following in tokens:
            if following == ')':
                return children
            children.append(read(following) if following == '(' else following)
        raise PDDLError('unclosed_expression')
    tree = read(next(tokens, None))
    ensure(next(tokens, None) is None, 'trailing_expression_or_token')
    return tree


def declarations(tokens, types, variables=False):
    ensure(isinstance(tokens, list), 'declaration_list')
    pairs, pending = [], []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == '-':
            ensure(pending and index + 1 < len(tokens), 'bad_type_separator')
            kind = tokens[index + 1]
            ensure(isinstance(kind, str) and kind in types, 'unknown_declared_type')
            pairs.extend((name, kind) for name in pending)
            pending = []
            index += 2
        else:
            pending.append(identifier(token, variables))
            index += 1
    pairs.extend((name, 'object') for name in pending)
    ensure(len({name for name, _ in pairs}) == len(pairs), 'duplicate_declared_name')
    return tuple(pairs)


def positive_conjuncts(expression):
    ensure(isinstance(expression, list) and expression, 'expected_fact_or_conjunction')
    return expression[1:] if expression[0] == 'and' else [expression]


def fact(expression, predicates, terms):
    ensure(isinstance(expression, list) and expression, 'expected_atom')
    name = identifier(expression[0])
    ensure(name in predicates, 'undeclared_predicate:' + name)
    kinds = predicates[name]
    ensure(len(expression) == len(kinds) + 1, 'atom_arity:' + name)
    for term, expected in zip(expression[1:], kinds):
        ensure(isinstance(term, str) and term in terms, 'undeclared_term:' + repr(term))
        ensure(expected == 'object' or terms[term] == expected, 'atom_argument_type:' + name)
    return tuple(expression)


@dataclass(frozen=True)
class Operator:
    parameters: tuple
    required: frozenset
    positive: frozenset
    negative: frozenset


@dataclass(frozen=True)
class Domain:
    name: str
    types: frozenset
    predicates: dict
    operators: dict


@dataclass(frozen=True)
class Problem:
    name: str
    objects: dict
    initial: frozenset
    goals: frozenset


def header(tree, expected):
    ensure(isinstance(tree, list) and len(tree) >= 2 and tree[0] == 'define', 'define_header')
    item = tree[1]
    ensure(isinstance(item, list) and len(item) == 2 and item[0] == expected, expected + '_header')
    return identifier(item[1])


def parse_domain(raw):
    tree = parse_parentheses(raw)
    name = header(tree, 'domain')
    fields, action_nodes = {}, []
    for node in tree[2:]:
        ensure(isinstance(node, list) and node, 'domain_clause')
        if node[0] == ':action':
            action_nodes.append(node)
        else:
            ensure(node[0] in (':requirements', ':types', ':predicates'), 'unsupported_domain_clause')
            ensure(node[0] not in fields, 'duplicate_domain_clause')
            fields[node[0]] = node[1:]
    ensure(':requirements' in fields and ':predicates' in fields, 'missing_domain_declaration')
    requirements = fields[':requirements']
    ensure(all(isinstance(x,str) for x in requirements), 'requirements_symbols')
    ensure(len(requirements) == len(set(requirements)) and ':strips' in requirements
           and set(requirements) <= {':strips', ':typing'}, 'unsupported_requirements')
    extra_types = [identifier(x) for x in fields.get(':types', [])]
    ensure(len(set(extra_types)) == len(extra_types) and 'object' not in extra_types, 'type_redefinition')
    ensure(not extra_types or ':typing' in requirements, 'types_require_typing')
    types = frozenset(['object', *extra_types])
    predicates = {}
    for node in fields[':predicates']:
        ensure(isinstance(node,list) and node, 'predicate_declaration')
        predicate = identifier(node[0])
        ensure(predicate not in predicates, 'duplicate_predicate')
        predicates[predicate] = tuple(kind for _, kind in declarations(node[1:], types, True))
    operators = {}
    for node in action_nodes:
        ensure(len(node) == 8 and node[2] == ':parameters'
               and node[4] == ':precondition' and node[6] == ':effect', 'unsupported_action_fields')
        action = identifier(node[1])
        ensure(action not in operators, 'duplicate_action')
        parameters = declarations(node[3], types, True)
        terms = dict(parameters)
        required = frozenset(fact(x, predicates, terms) for x in positive_conjuncts(node[5]))
        positive, negative = set(), set()
        for effect in positive_conjuncts(node[7]):
            ensure(isinstance(effect,list) and effect, 'effect_expression')
            if effect[0] == 'not':
                ensure(len(effect) == 2, 'negative_effect_arity')
                negative.add(fact(effect[1], predicates, terms))
            else:
                positive.add(fact(effect, predicates, terms))
        operators[action] = Operator(parameters, required, frozenset(positive), frozenset(negative))
    return Domain(name, types, predicates, operators)


def parse_problem(raw, domain):
    tree = parse_parentheses(raw)
    name = header(tree, 'problem')
    fields = {}
    for node in tree[2:]:
        ensure(isinstance(node,list) and node and node[0] in (':domain', ':objects', ':init', ':goal'), 'unsupported_problem_clause')
        ensure(node[0] not in fields, 'duplicate_problem_clause')
        fields[node[0]] = node[1:]
    ensure(set(fields) == {':domain', ':objects', ':init', ':goal'}, 'incomplete_problem')
    ensure(fields[':domain'] == [domain.name], 'problem_domain_mismatch')
    ensure(len(fields[':goal']) == 1, 'one_goal_expression')
    objects = dict(declarations(fields[':objects'], domain.types))
    initial = frozenset(fact(x, domain.predicates, objects) for x in fields[':init'])
    goals = frozenset(fact(x, domain.predicates, objects) for x in positive_conjuncts(fields[':goal'][0]))
    return Problem(name, objects, initial, goals)


def unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ActionDocumentError('duplicate_json_key')
        result[key] = value
    return result


def forbidden_number(value):
    raise ActionDocumentError('number_not_allowed')


def parse_actions(raw):
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=unique_json_pairs,
                           parse_int=forbidden_number, parse_float=forbidden_number,
                           parse_constant=forbidden_number)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ActionDocumentError('invalid_action_json:' + type(exc).__name__) from exc
    if type(value) is not dict or set(value) != {'actions'} or type(value['actions']) is not list:
        raise ActionDocumentError('expected_exact_actions_object')
    answer = []
    for row in value['actions']:
        if type(row) is not list or not row or any(type(x) is not str for x in row):
            raise ActionDocumentError('action_must_be_nonempty_string_array')
        # The actor contract permits ASCII case-insensitive PDDL identifiers.
        answer.append(tuple(x.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')) for x in row))
    return tuple(answer)


def state_digest(facts):
    raw = json.dumps(sorted(facts), ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return {'fact_count': len(facts), 'sha256': hashlib.sha256(raw).hexdigest()}


def replay(domain, problem, actions, observer=None):
    """Return explicit legality/final-goal evidence; never search for an action."""
    current = set(problem.initial)
    completed = 0
    for position, row in enumerate(actions, 1):
        name, arguments = row[0], row[1:]
        event = {'step': position, 'action': list(row), 'before': state_digest(current)}
        failure = None
        if name not in domain.operators:
            failure = 'unknown_action'
        else:
            operator = domain.operators[name]
            if len(arguments) != len(operator.parameters):
                failure = 'wrong_action_arity'
            elif any(arg not in problem.objects for arg in arguments):
                failure = 'unknown_object'
            elif any(kind != 'object' and problem.objects[arg] != kind
                     for (_,kind),arg in zip(operator.parameters,arguments)):
                failure = 'wrong_argument_type'
            else:
                substitution = dict(zip((var for var,_ in operator.parameters),arguments))
                def instantiate(atoms):
                    return {tuple(substitution.get(term,term) for term in atom) for atom in atoms}
                required = instantiate(operator.required)
                absent = required.difference(current)
                event['required_preconditions'] = sorted(required)
                if absent:
                    failure = 'unsatisfied_precondition'
                    event['missing_preconditions'] = sorted(absent)
                else:
                    removed = instantiate(operator.negative)
                    inserted = instantiate(operator.positive)
                    event['deletes'], event['adds'] = sorted(removed), sorted(inserted)
                    event['add_delete_overlap'] = sorted(removed.intersection(inserted))
                    current.difference_update(removed)
                    current.update(inserted)
                    completed += 1
                    event['after'] = state_digest(current)
        if failure:
            event['failure'] = failure
        if observer:
            observer(event)
        if failure:
            return {'witness_valid':False,'reason':failure,'failed_step':position,
                    'assigned_actions':len(actions),'completed_actions':completed,
                    'missing_goals':sorted(problem.goals.difference(current)),
                    'final_pin':state_digest(current)}, frozenset(current)
    missing = problem.goals.difference(current)
    return {'witness_valid':not missing,'reason':'missing_final_goals' if missing else None,
            'failed_step':None,'assigned_actions':len(actions),'completed_actions':completed,
            'missing_goals':sorted(missing),'final_pin':state_digest(current)}, frozenset(current)
