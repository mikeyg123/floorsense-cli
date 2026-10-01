"""Static, hand-generated completion scripts for bash/zsh/fish.

`fs`'s own parsing (`args.py`) does free-order token classification, not
argparse subparsers, so there's no completion library to hook into, and
execing `fs` on every keystroke would be slow and need a live session
just to complete a flag. Instead these scripts are pure shell, completing
only subcommand names and flags -- both interpolated from `cli.py`'s
`COMMANDS`/`build_parser()` at generation time (by `cli.main`), so
`fs completion <shell>` can't drift from the real command/flag lists the
way a hand-maintained wordlist would. Desk/date/name completion would
need a live `fs` process and isn't attempted here.
"""

SHELLS = ("bash", "zsh", "fish")


def _bash(commands, flags):
    return f"""\
# fs completion for bash
#
# Install (persists across shells):
#   fs completion bash >> ~/.bash_completion
# or, to try it in the current shell only:
#   source <(fs completion bash)
#
# Regenerate (re-run the install line) after upgrading fs, to pick up
# any new commands or options.

_fs_completions() {{
    local cur
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    local commands="{' '.join(commands)}"
    local options="{' '.join(flags)}"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=($(compgen -W "$options" -- "$cur"))
    elif [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$commands" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$commands $options" -- "$cur"))
    fi
}}
complete -F _fs_completions fs
"""


def _zsh(commands, flags):
    return f"""\
#compdef fs
#
# fs completion for zsh
#
# Install:
#   fs completion zsh > "${{fpath[1]}}/_fs"
# (any directory already on $fpath works -- run `echo $fpath` to see them)
# then start a new shell, or run: autoload -U compinit && compinit
#
# Regenerate (re-run the install line) after upgrading fs, to pick up
# any new commands or options.

_fs() {{
    local -a commands options
    commands=({' '.join(commands)})
    options=({' '.join(flags)})
    if [[ "$words[CURRENT]" == -* ]]; then
        compadd -a options
    else
        compadd -a commands
    fi
}}

_fs "$@"
"""


def _fish(commands, flags):
    return f"""\
# fs completion for fish
#
# Install:
#   fs completion fish > ~/.config/fish/completions/fs.fish
# fish loads completions automatically in every new shell -- no other
# step needed.
#
# Regenerate (re-run the install line) after upgrading fs, to pick up
# any new commands or options.

complete -c fs -f
complete -c fs -n '__fish_use_subcommand' -a '{' '.join(commands)}'
complete -c fs -a '{' '.join(flags)}'
"""


_RENDERERS = {"bash": _bash, "zsh": _zsh, "fish": _fish}


def render(shell, commands, flags):
    """`shell` must already be validated against `SHELLS`."""
    return _RENDERERS[shell](commands, flags)
