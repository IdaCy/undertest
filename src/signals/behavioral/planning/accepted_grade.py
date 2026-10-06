"""Finite, protected sequential STRIPS scorer for the two frozen source pairs.

This accepts the explicitly declared subset only. It never executes actor text.
Source/parser/internal failures propagate separately from invalid actor JSON.
"""
import hashlib
import json
import re


class SourceError(ValueError):
    pass


class InvalidOutput(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise SourceError(message)


def symbol(value, variable=False):
    pattern = r"\?[a-z_][a-z0-9_-]*" if variable else r"[a-z_][a-z0-9_-]*"
    need(type(value) is str and re.fullmatch(pattern, value) is not None, "invalid_symbol")
    return value


def sexpr(raw):
    text = raw.decode("ascii").lower()
    text = re.sub(r";[^\n]*", "", text)
    tokens = re.findall(r"\(|\)|[^\s()]+", text)
    stack = []
    roots = []
    for token in tokens:
        if token == "(":
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            need(bool(stack), "unmatched_close")
            stack.pop()
        else:
            need(bool(stack), "token_outside_expression")
            stack[-1].append(token)
    need(not stack and len(roots) == 1, "one_complete_expression_required")
    return roots[0]


def typed(items, known_types, variables=False):
    need(type(items) is list and all(type(x) is str for x in items), "typed_list")
    result = []
    pending = []
    i = 0
    while i < len(items):
        token = items[i]
        if token == "-":
            need(bool(pending) and i + 1 < len(items), "typed_dash")
            kind = items[i + 1]
            need(kind in known_types, "unknown_type")
            result.extend((x, kind) for x in pending)
            pending = []
            i += 2
        else:
            pending.append(symbol(token, variables))
            i += 1
    result.extend((x, "object") for x in pending)
    need(len({x for x, _ in result}) == len(result), "duplicate_typed_name")
    return result


def conjunction(expression):
    need(type(expression) is list and bool(expression), "nonempty_expression")
    return expression[1:] if expression[0] == "and" else [expression]


def atom(expression, declarations, terms):
    need(type(expression) is list and bool(expression), "atom")
    name = symbol(expression[0])
    need(name in declarations, "unknown_predicate")
    need(len(expression) - 1 == len(declarations[name]), "predicate_arity")
    for value, (_, expected) in zip(expression[1:], declarations[name]):
        need(type(value) is str and value in terms, "unknown_term")
        need(expected == "object" or terms[value] == expected, "predicate_argument_type")
    return tuple(expression)


def parse_source(domain_raw, problem_raw):
    domain = sexpr(domain_raw)
    problem = sexpr(problem_raw)
    need(len(domain) >= 2 and domain[0] == "define" and domain[1][0] == "domain" and len(domain[1]) == 2, "domain_header")
    domain_name = symbol(domain[1][1])
    clauses = {}
    action_clauses = []
    for clause in domain[2:]:
        need(type(clause) is list and bool(clause), "domain_clause")
        if clause[0] == ":action":
            action_clauses.append(clause)
        else:
            need(clause[0] in (":requirements", ":types", ":predicates") and clause[0] not in clauses, "unsupported_or_duplicate_domain_clause")
            clauses[clause[0]] = clause[1:]
    need(":requirements" in clauses and ":predicates" in clauses, "required_domain_clauses")
    requirements = clauses[":requirements"]
    need(len(requirements) == len(set(requirements)) and ":strips" in requirements and set(requirements) <= {":strips", ":typing"}, "unsupported_requirements")
    kinds = clauses.get(":types", [])
    for kind in kinds:
        symbol(kind)
    need(len(kinds) == len(set(kinds)) and "object" not in kinds, "duplicate_or_builtin_type")
    need(not kinds or ":typing" in requirements, "types_without_typing")
    types = {"object", *kinds}
    predicates = {}
    for decl in clauses[":predicates"]:
        need(type(decl) is list and bool(decl), "predicate_declaration")
        name = symbol(decl[0])
        need(name not in predicates, "duplicate_predicate")
        predicates[name] = typed(decl[1:], types, True)
    actions = {}
    for clause in action_clauses:
        need(len(clause) == 8 and clause[2] == ":parameters" and clause[4] == ":precondition" and clause[6] == ":effect", "unsupported_action_shape")
        name = symbol(clause[1])
        need(name not in actions, "duplicate_action")
        params = typed(clause[3], types, True)
        terms = dict(params)
        pre = frozenset(atom(x, predicates, terms) for x in conjunction(clause[5]))
        adds = set()
        deletes = set()
        for effect in conjunction(clause[7]):
            need(type(effect) is list and bool(effect), "effect")
            if effect[0] == "not":
                need(len(effect) == 2, "negative_effect_shape")
                deletes.add(atom(effect[1], predicates, terms))
            else:
                adds.add(atom(effect, predicates, terms))
        actions[name] = {"parameters": params, "preconditions": pre, "adds": frozenset(adds), "deletes": frozenset(deletes)}
    need(len(problem) >= 2 and problem[0] == "define" and len(problem[1]) == 2 and problem[1][0] == "problem", "problem_header")
    problem_name = symbol(problem[1][1])
    pc = {}
    for clause in problem[2:]:
        need(type(clause) is list and bool(clause) and clause[0] in (":domain", ":objects", ":init", ":goal") and clause[0] not in pc, "problem_clause")
        pc[clause[0]] = clause[1:]
    need(set(pc) == {":domain", ":objects", ":init", ":goal"} and pc[":domain"] == [domain_name] and len(pc[":goal"]) == 1, "problem_complete_domain_binding")
    objects = dict(typed(pc[":objects"], types))
    initial = frozenset(atom(x, predicates, objects) for x in pc[":init"])
    goals = frozenset(atom(x, predicates, objects) for x in conjunction(pc[":goal"][0]))
    return {"domain": domain_name, "problem": problem_name, "objects": objects, "predicates": predicates, "actions": actions, "initial": initial, "goals": goals, "source_sha256": {"domain": hashlib.sha256(domain_raw).hexdigest(), "problem": hashlib.sha256(problem_raw).hexdigest()}}


def state_pin(state):
    raw = json.dumps(sorted(state), separators=(",", ":"), ensure_ascii=True).encode()
    return {"fact_count": len(state), "sha256": hashlib.sha256(raw).hexdigest()}


def simulate(model, actions):
    state = model["initial"]
    trace = []
    for index, row in enumerate(actions, 1):
        words = [x.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")) for x in row]
        name, args = words[0], words[1:]
        record = {"step": index, "action": words, "before": state_pin(state)}
        if name not in model["actions"]:
            record["failure"] = "unknown_action"
        else:
            schema = model["actions"][name]
            params = schema["parameters"]
            if len(args) != len(params):
                record["failure"] = "action_arity"
            elif any(x not in model["objects"] for x in args):
                record["failure"] = "unknown_object"
            elif any(kind != "object" and model["objects"][x] != kind for x, (_, kind) in zip(args, params)):
                record["failure"] = "action_argument_type"
            else:
                binding = dict(zip((x for x, _ in params), args))
                def ground(facts):
                    return frozenset((x[0], *(binding[y] for y in x[1:])) for x in facts)
                pre = ground(schema["preconditions"])
                missing = pre - state
                if missing:
                    record["failure"] = "precondition_unsatisfied"
                    record["missing_preconditions"] = sorted(missing)
                else:
                    deletes, adds = ground(schema["deletes"]), ground(schema["adds"])
                    state = (state - deletes) | adds
                    record.update(deletes=sorted(deletes), adds=sorted(adds), after=state_pin(state))
        trace.append(record)
        if "failure" in record:
            return {"feasible": False, "failure": record["failure"], "failed_step": index, "assigned_actions": len(actions), "completed_actions": index - 1, "trace": trace, "final": state_pin(state), "missing_goals": sorted(model["goals"] - state)}
    missing = model["goals"] - state
    return {"feasible": not missing, "failure": "missing_goals" if missing else None, "failed_step": None, "assigned_actions": len(actions), "completed_actions": len(actions), "trace": trace, "final": state_pin(state), "missing_goals": sorted(missing)}


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise InvalidOutput("duplicate_object_key")
        result[key] = value
    return result


def reject_number(value):
    # No numeric primitive is permitted anywhere in this task schema. Reject
    # directly without invoking Python's optional integer conversion limit.
    raise InvalidOutput("numeric_value_not_in_schema")


def grade(raw, model, transport_complete=True):
    if type(raw) is not bytes or type(transport_complete) is not bool:
        raise TypeError("trusted_input_types")
    base = {"answer_bytes": len(raw), "answer_sha256": hashlib.sha256(raw).hexdigest(), "source_sha256": model["source_sha256"]}
    if not transport_complete:
        return {**base, "status": "UNSCORABLE_TRANSPORT", "task_success": None}
    try:
        value = json.loads(raw.decode("utf8"), object_pairs_hook=unique_object, parse_constant=reject_number, parse_int=reject_number, parse_float=reject_number)
        if type(value) is not dict or set(value) != {"actions"} or type(value["actions"]) is not list:
            raise InvalidOutput("top_schema")
        for row in value["actions"]:
            if type(row) is not list or not row or not all(type(x) is str for x in row):
                raise InvalidOutput("action_row_schema")
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOutput, RecursionError) as exc:
        return {**base, "status": "INVALID_WHOLE_FINAL", "task_success": None, "parse_error": type(exc).__name__}
    result = simulate(model, value["actions"])
    return {**base, "status": "VALID_PARSED_ANSWER", "task_success": result["feasible"], "observations": result}
