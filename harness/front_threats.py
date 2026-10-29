"""Expected border threats from public diplomacy; these are not declarations of war."""


def _tags(specs):
    if isinstance(specs, str):
        specs = [specs]
    return {tag.strip().upper() for spec in specs or () if isinstance(spec, str)
            for tag in spec.split("+") if tag.strip()}


def coalition(g, tags):
    """The given countries and their current faction members, using pure public reads.

    A faction partner may join its war; reporting that border does not say it has joined.
    If diplomacy cannot be read, retain the explicit countries rather than lose a front.
    """
    seeds = _tags(tags)
    out = set(seeds)
    for tag in sorted(seeds):
        try:
            out |= _tags(g.faction_members(tag).get("members") or ())
        except Exception:
            continue
    return out


def expected(ctx):
    """The runner's public threat snapshot, or explicit expectations before it exists.

    This function never opens the game. The runner expands these explicit tags through
    coalition() on its gateway and stores the result as expected_enemies for this turn.
    """
    if "expected_enemies" in ctx:
        return _tags(ctx["expected_enemies"])
    out = _tags(ctx.get("enemies")) | _tags(ctx.get("coming_wars")) | _tags(ctx.get("war_wanted"))
    plan = getattr(ctx.get("planner"), "plan", None) or {}
    for war in plan.get("wars") or ():
        out |= _tags(war.get("enemy"))
    for marshal in (ctx.get("marshals") or {}).values():
        out |= _tags(marshal.theatre.get("enemy"))
    return out


