"""`fs completion <shell>` and the underlying `completion.render`.

Completion never logs in or touches config, so these tests exercise
`main` without needing the `cfgdir` fixture other command tests rely on.
"""

from fs_cli import completion
from fs_cli.cli import main
from fs_cli.errors import ExitCode


def test_bash_completion_has_install_comment_and_commands(tmp_path):
    assert main(["completion", "bash"], directory=tmp_path) == ExitCode.OK


def test_completion_render_bash(capsys, tmp_path):
    main(["completion", "bash"], directory=tmp_path)
    out = capsys.readouterr().out
    assert ">> ~/.bash_completion" in out
    assert "book" in out and "--json" in out


def test_completion_render_zsh(capsys, tmp_path):
    main(["completion", "zsh"], directory=tmp_path)
    out = capsys.readouterr().out
    assert out.startswith("#compdef fs")
    assert '${fpath[1]}/_fs' in out
    assert "book" in out and "--json" in out


def test_completion_render_fish(capsys, tmp_path):
    main(["completion", "fish"], directory=tmp_path)
    out = capsys.readouterr().out
    assert "~/.config/fish/completions/fs.fish" in out
    assert "book" in out and "--json" in out


def test_completion_includes_help_and_completion_commands(capsys, tmp_path):
    main(["completion", "bash"], directory=tmp_path)
    out = capsys.readouterr().out
    assert "help" in out
    assert "completion" in out


def test_completion_with_no_shell_is_a_usage_error(capsys, tmp_path):
    assert main(["completion"], directory=tmp_path) == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "shell name is required" in err
    assert "bash, zsh, fish" in err


def test_completion_with_unknown_shell_is_a_usage_error(capsys, tmp_path):
    assert main(["completion", "pwsh"], directory=tmp_path) == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "unknown shell 'pwsh'" in err
    assert "bash, zsh, fish" in err


# --- completion.render directly ---------------------------------------

def test_render_bash_embeds_commands_and_flags():
    text = completion.render("bash", ("status", "book"), ("--json", "-y"))
    assert "status book" in text
    assert "--json -y" in text


def test_render_zsh_embeds_commands_and_flags():
    text = completion.render("zsh", ("status", "book"), ("--json", "-y"))
    assert "commands=(status book)" in text
    assert "options=(--json -y)" in text


def test_render_fish_embeds_commands_and_flags():
    text = completion.render("fish", ("status", "book"), ("--json", "-y"))
    assert "-a 'status book'" in text
    assert "-a '--json -y'" in text
