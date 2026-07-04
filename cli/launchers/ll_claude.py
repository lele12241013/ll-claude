"""ll-claude: launches the Orchestrator in the current terminal and opens
all 6 specialist agents in separate Windows Terminal tabs automatically.

Usage:
    ll-claude                    # start a fresh multi-agent session
    ll-claude --resume           # reuse the last session workspace
    ll-claude -p "Build an API"  # pass a first message straight to the orchestrator
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from api.admin_urls import local_proxy_root_url
from cli.claude_env import (
    CLAUDE_BINARY_NAME,
    CLAUDE_CODE_AUTO_COMPACT_WINDOW,
    claude_auth_token,
)
from config.settings import get_settings

from .claude import build_claude_launcher_env
from .common import preflight_proxy, resolve_client_binary, run_client_process

# ── specialist agents ─────────────────────────────────────────────────────────
_SPECIALISTS: list[tuple[str, str]] = [
    (
        "Frontend",
        "Programador Front-end – implementa toda a interface do usuário: HTML, CSS, "
        "JavaScript/TypeScript, frameworks (React, Vue, etc.), responsividade e integração "
        "com a API do Back-end.",
    ),
    (
        "Backend",
        "Programador Back-end – implementa o servidor, APIs REST/GraphQL, banco de dados, "
        "autenticação, regras de negócio e integrações externas.",
    ),
    (
        "Revisor",
        "Revisor de Código – lê todo o código escrito pelos colegas, aponta bugs, code "
        "smells, problemas de segurança, sugere refatorações e garante padrões de qualidade.",
    ),
    (
        "Testador",
        "Testador (QA) – escreve testes unitários, de integração e end-to-end; executa "
        "cenários de teste; reporta falhas com reprodução mínima para os colegas corrigirem.",
    ),
    (
        "Documentador",
        "Documentador Técnico – escreve README, docstrings, comentários, diagramas de "
        "arquitetura, exemplos de uso da API e guias de instalação/deploy.",
    ),
    (
        "Executor",
        "Executor / DevOps – roda os comandos no terminal, configura ambiente, Docker, "
        "CI/CD, variáveis de ambiente, scripts de build/deploy e resolve problemas de "
        "infraestrutura.",
    ),
]

_DISPLAY_NAME = "Claude Code"
_INSTALL_HINT = "Install Claude Code with: npm install -g @anthropic-ai/claude-code"

# Stable workspace dir so the board persists between calls; each session gets
# a timestamped subdirectory unless --resume is passed.
_WORKSPACE_ROOT = Path.home() / ".fcc" / "agents"

# Sentinel file that stores the last session path for --resume.
_LAST_SESSION_FILE = _WORKSPACE_ROOT / ".last_session"


# ── workspace helpers ─────────────────────────────────────────────────────────

def _session_dir(resume: bool) -> Path:
    """Return (and create) the session directory."""
    _WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    if resume and _LAST_SESSION_FILE.exists():
        last = Path(_LAST_SESSION_FILE.read_text(encoding="utf-8").strip())
        if last.is_dir():
            return last

    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    session = _WORKSPACE_ROOT / ts
    session.mkdir(parents=True, exist_ok=True)
    _LAST_SESSION_FILE.write_text(str(session), encoding="utf-8")
    return session


def _board_file(session: Path) -> Path:
    board = session / "shared" / "BOARD.md"
    board.parent.mkdir(parents=True, exist_ok=True)
    if not board.exists():
        board.write_text(
            "# Quadro de Mensagens\n"
            "# Os agentes lêem e escrevem aqui para se comunicar.\n\n"
            "## Protocolo\n"
            "- Orquestrador escreve:   [TAREFA para NomeDoAgente]: descrição\n"
            "- Especialista responde:  [NomeDoAgente CONCLUÍDO]: resumo\n\n",
            encoding="utf-8",
        )
    return board


def _write_specialist_context(
    work_dir: Path,
    name: str,
    role: str,
    board: Path,
    specialists: list[tuple[str, str]],
) -> None:
    """Write AGENT_CONTEXT.md for a specialist."""
    work_dir.mkdir(parents=True, exist_ok=True)
    others = "\n".join(
        f"  - {n}: {r[:60]}…" if len(r) > 60 else f"  - {n}: {r}"
        for n, r in [("Orquestrador", "Agente de conversa e coordenador da equipe")]
        + [s for s in specialists if s[0] != name]
    )
    ctx = (
        f"# Agente: {name}\n\n"
        f"## Sua função\n{role}\n\n"
        f"## Sua equipe\n{others}\n\n"
        f"## Quadro de mensagens compartilhado\n{board}\n\n"
        f"## Como trabalhar\n"
        f"1. Leia o quadro de mensagens acima.\n"
        f"2. Procure tarefas no formato  [TAREFA para {name}]: descrição\n"
        f"3. Execute a tarefa no seu diretório: {work_dir}\n"
        f"4. Quando terminar, escreva no quadro:  [{name} CONCLUÍDO]: resumo\n"
        f"5. Fique verificando o quadro periodicamente para novas tarefas.\n\n"
        f"## Seu diretório de trabalho\n{work_dir}\n"
    )
    (work_dir / "AGENT_CONTEXT.md").write_text(ctx, encoding="utf-8")


def _write_orchestrator_context(
    work_dir: Path,
    board: Path,
    specialists: list[tuple[str, str]],
) -> None:
    """Write AGENT_CONTEXT.md for the Orchestrator."""
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_list = "\n".join(f"  - {n}: {r[:70]}…" if len(r) > 70 else f"  - {n}: {r}" for n, r in specialists)
    ctx = (
        f"# Agente: Orquestrador\n\n"
        f"## Sua função\n"
        f"Você é o agente de conversa e coordenador da equipe.\n"
        f"O usuário fala com você. Você analisa os pedidos, divide em subtarefas\n"
        f"e as distribui para os especialistas via quadro de mensagens.\n\n"
        f"## Sua equipe de especialistas\n{spec_list}\n\n"
        f"## Quadro de mensagens compartilhado\n{board}\n\n"
        f"## Como coordenar\n"
        f"1. Receba o pedido do usuário.\n"
        f"2. Planeje quais especialistas precisam agir.\n"
        f"3. Escreva no quadro as tarefas no formato:\n"
        f"     [TAREFA para Frontend]: implemente o componente de login\n"
        f"     [TAREFA para Backend]: crie o endpoint POST /auth/login\n"
        f"4. Monitore o quadro para coletar os resultados ([NomeDoAgente CONCLUÍDO]).\n"
        f"5. Consolide os resultados e responda ao usuário.\n\n"
        f"## Seu diretório de trabalho\n{work_dir}\n"
    )
    (work_dir / "AGENT_CONTEXT.md").write_text(ctx, encoding="utf-8")


# ── terminal launchers ────────────────────────────────────────────────────────

def _open_specialist_tabs(
    specialists: list[tuple[str, str]],
    session: Path,
    board: Path,
    base_token: str,
    proxy_root_url: str,
) -> None:
    """Open one Windows Terminal tab per specialist agent."""

    has_wt = shutil.which("wt") is not None
    # Prefer PowerShell 7 (pwsh) but fall back to Windows PowerShell 5 (powershell)
    ps_exe = "pwsh" if shutil.which("pwsh") else "powershell"

    def _encode_command(ps_script: str) -> str:
        """Encode a PowerShell script as Base64 for -EncodedCommand."""
        return base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")

    def _make_ps_script(name: str, work_dir: Path) -> str:
        token = f"{base_token}:{name.lower()}"
        return (
            f"$env:ANTHROPIC_BASE_URL = '{proxy_root_url}'\n"
            f"$env:ANTHROPIC_AUTH_TOKEN = '{token}'\n"
            f"$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = '1'\n"
            f"$env:CLAUDE_CODE_AUTO_COMPACT_WINDOW = '{CLAUDE_CODE_AUTO_COMPACT_WINDOW}'\n"
            f"Set-Location '{work_dir}'\n"
            f"Write-Host '=== {name} ===' -ForegroundColor Green\n"
            f"Write-Host 'Quadro: {board}' -ForegroundColor Yellow\n"
            f"Write-Host 'Diga: Read AGENT_CONTEXT.md' -ForegroundColor Yellow\n"
            f"Write-Host ''\n"
            f"claude --dangerously-skip-permissions\n"
        )

    if has_wt:
        wt_args = ["wt", "-w", "0"]

        for i, (name, _role) in enumerate(specialists):
            work_dir = session / f"agent_{i + 1}_{name.lower()}"
            work_dir.mkdir(parents=True, exist_ok=True)
            encoded = _encode_command(_make_ps_script(name, work_dir))
            wt_args += [";", "new-tab", "--title", name,
                        ps_exe, "-NoExit", "-EncodedCommand", encoded]

        subprocess.Popen(wt_args, shell=False)

    else:
        # Fallback: separate PowerShell console windows
        for i, (name, _role) in enumerate(specialists):
            work_dir = session / f"agent_{i + 1}_{name.lower()}"
            work_dir.mkdir(parents=True, exist_ok=True)
            encoded = _encode_command(_make_ps_script(name, work_dir))
            subprocess.Popen(
                [ps_exe, "-NoExit", "-EncodedCommand", encoded],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )


# ── main entry point ──────────────────────────────────────────────────────────

def launch(argv: list[str] | None = None) -> None:
    """Launch ll-claude: Orchestrator here + specialists in new tabs."""

    args = list(sys.argv[1:] if argv is None else argv)

    # Parse --resume flag (consumed here, not passed to claude)
    resume = "--resume" in args
    if resume:
        args.remove("--resume")

    settings      = get_settings()
    proxy_root_url = local_proxy_root_url(settings)

    # Check proxy is running
    if error := preflight_proxy(proxy_root_url):
        print(
            f"Proxy não está rodando em {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Inicie em outro terminal com: ll-server", file=sys.stderr)
        raise SystemExit(1)

    binary_path = resolve_client_binary(
        binary_name=CLAUDE_BINARY_NAME,
        display_name=_DISPLAY_NAME,
        install_hint=_INSTALL_HINT,
    )

    # ── prepare workspace ─────────────────────────────────────────────────────
    session = _session_dir(resume)
    board   = _board_file(session)

    orch_work_dir = session / "agent_0_orquestrador"
    _write_orchestrator_context(orch_work_dir, board, _SPECIALISTS)

    for i, (name, role) in enumerate(_SPECIALISTS):
        work_dir = session / f"agent_{i + 1}_{name.lower()}"
        _write_specialist_context(work_dir, name, role, board, _SPECIALISTS)

    # ── open specialist tabs ──────────────────────────────────────────────────
    base_token = claude_auth_token(settings.anthropic_auth_token)

    print("\033[1m=== ll-claude: Sistema Multi-Agente ===\033[0m")
    print(f"  Workspace : {session}")
    print(f"  Quadro    : {board}")
    print(f"  Agentes   : Orquestrador + {len(_SPECIALISTS)} especialistas")
    print()
    print("Abrindo abas dos especialistas…")

    _open_specialist_tabs(_SPECIALISTS, session, board, base_token, proxy_root_url)

    print()
    print("\033[1;96m=== ORQUESTRADOR (esta janela) ===\033[0m")
    print(f"\033[93mDiga ao claude: 'Read AGENT_CONTEXT.md e comece a coordenar'\033[0m")
    print()

    # ── launch orchestrator in this terminal ──────────────────────────────────
    orch_token = f"{base_token}:orquestrador"

    env = build_claude_launcher_env(
        proxy_root_url=proxy_root_url,
        auth_token=orch_token,
        base_env=os.environ,
    )

    # Set terminal title to identify this as the Orchestrator
    print("\033]0;Orquestrador\007", end="", flush=True)

    run_client_process(
        command=[binary_path, "--dangerously-skip-permissions", *args],
        env=env,
        binary_name=CLAUDE_BINARY_NAME,
        display_name=_DISPLAY_NAME,
        install_hint=_INSTALL_HINT,
        cwd=str(orch_work_dir),
    )
