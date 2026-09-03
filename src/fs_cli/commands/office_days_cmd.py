"""`fs office-days [<day>...]` -- a straight `config.toml` write, no
`plan.py` pipeline.

No args is read-only. Given args, this REPLACES the whole list rather
than offering `add`/`remove`/`set`/`delete` -- office days are a single
unordered set with no name to pick between.

Tokens are validated against `dates.WEEKDAYS` before anything is written
-- an unknown token is a `UsageError`, config left untouched. Valid
tokens are canonicalised to full lowercase name and sorted by weekday
index, so `config.toml` reads the same regardless of typed order/spelling.
"""

from .. import config as config_mod
from ..args import reject_forced
from ..dates import fmt_weekday, weekday_index
from ..errors import ExitCode, UsageError

__all__ = ["cmd_office_days"]


def cmd_office_days(ctx):
    out, cfg = ctx.out, ctx.config
    reject_forced(ctx.args, "fs office-days")
    tokens = list(ctx.args.args)

    if not tokens:
        _print_days(out, cfg.office_days)
        return ExitCode.OK

    indexes = set()
    for token in tokens:
        idx = weekday_index(token)
        if idx is None:
            raise UsageError(
                f"unknown day {token!r}",
                hint="Use monday..sunday, or a short form like mon/tues.")
        indexes.add(idx)

    cfg.office_days = [fmt_weekday(i).lower() for i in sorted(indexes)]
    config_mod.save(cfg, ctx.directory, on_repair=out.warn)
    _print_days(out, cfg.office_days)
    return ExitCode.OK


def _print_days(out, office_days):
    if office_days:
        names = [fmt_weekday(weekday_index(d)) for d in office_days]
        out.print(f"Office days: {', '.join(names)}")
    else:
        out.print("Office days: none configured")
    out.emit({"office_days": list(office_days)})
