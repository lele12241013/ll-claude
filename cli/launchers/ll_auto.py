"""ll-auto: Automatic multi-agent coordinator.

Runs a single-terminal loop:
  User → Orchestrator plans → Specialists execute in parallel → Consolidate → Response

Usage:
    ll-auto                    # interactive loop
    ll-auto -p "Build an API"  # single prompt then exit
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from api.admin_urls import local_proxy_root_url
from cli.claude_env import claude_auth_token
from cli.launchers.common import preflight_proxy
from config.settings import get_settings

# ── agent system prompts ──────────────────────────────────────────────────────

_SPECIALIST_NAMES = ["frontend", "backend", "revisor", "testador", "documentador", "executor"]

_SYSTEM_PROMPTS: dict[str, str] = {
    "orquestrador": (
        "Você é o Orquestrador de uma equipe de desenvolvimento. "
        "O usuário fala APENAS com você.\n\n"
        "REGRA PRINCIPAL — classifique a mensagem antes de agir:\n\n"
        "CONVERSA: responda diretamente, sem delegar.\n"
        "  - Saudações, perguntas gerais, dúvidas conceituais, agradecimentos.\n"
        "  - Exemplos: 'oi', 'o que você faz?', 'explica X', 'obrigado'.\n\n"
        "TAREFA: delegue aos especialistas usando o formato:\n"
        "  - Pedidos de criação, implementação, revisão, teste, documentação.\n"
        "  - Exemplos: 'crie uma API', 'refatore isso', 'escreva testes'.\n"
        "  - Use EXATAMENTE:\n"
        "      [TAREFA para Frontend]: descrição\n"
        "      [TAREFA para Backend]: descrição\n"
        "      [TAREFA para Revisor]: descrição\n"
        "      [TAREFA para Testador]: descrição\n"
        "      [TAREFA para Documentador]: descrição\n"
        "      [TAREFA para Executor]: descrição\n\n"
        "Delegue APENAS para especialistas relevantes à tarefa. "
        "Após receber os resultados, consolide e apresente ao usuário."
    ),
    "frontend": (
        "Você é o especialista Frontend. Recebeu uma tarefa específica do Orquestrador. "
        "Execute-a e responda APENAS com o resultado técnico (código, análise, etc.). Seja direto e conciso."
    ),
    "backend": (
        "Você é o especialista Backend. Recebeu uma tarefa específica do Orquestrador. "
        "Execute-a e responda APENAS com o resultado técnico (código, análise, etc.). Seja direto e conciso."
    ),
    "revisor": (
        "Você é o Revisor de Código. Recebeu uma tarefa específica do Orquestrador. "
        "Execute-a e responda APENAS com o resultado técnico (revisão, problemas encontrados, sugestões). Seja direto."
    ),
    "testador": (
        "Você é o Testador QA. Recebeu uma tarefa específica do Orquestrador. "
        "Execute-a e responda APENAS com o resultado técnico (testes, cenários, bugs). Seja direto."
    ),
    "documentador": (
        "Você é o Documentador Técnico. Recebeu uma tarefa específica do Orquestrador. "
        "Execute-a e responda APENAS com o resultado técnico (documentação, README, etc.). Seja direto."
    ),
    "executor": (
        "Você é o Executor/DevOps. Recebeu uma tarefa específica do Orquestrador. "
        "Execute-a e responda APENAS com o resultado técnico (comandos, scripts, configurações). Seja direto."
    ),
}

# Matches: [TAREFA para NomeDoAgente]: description (until next [TAREFA or end)
_TASK_RE = re.compile(
    r"\[TAREFA para ([^\]]+)\]:\s*(.+?)(?=\[TAREFA para|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# ── HTTP helper ───────────────────────────────────────────────────────────────

async def _chat(
    client: Any,
    proxy_root_url: str,
    base_token: str,
    role: str,
    messages: list[dict],
    model: str = "claude-sonnet-4-20250514",
) -> str:
    """Send a chat request to the proxy for a specific agent role, parsing SSE stream."""
    import json as _json

    token = f"{base_token}:{role}"
    url = f"{proxy_root_url.rstrip('/')}/v1/messages"
    payload = {
        "model": model,
        "max_tokens": 8096,
        "stream": True,
        "system": _SYSTEM_PROMPTS[role],
        "messages": messages,
    }
    headers = {
        "x-api-key": token,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    text_parts: list[str] = []
    async with client.stream("POST", url, json=payload, headers=headers, timeout=120) as response:
        if response.status_code != 200:
            body = await response.aread()
            raise RuntimeError(f"HTTP {response.status_code}: {body[:300]}")
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event = _json.loads(raw)
            except _json.JSONDecodeError:
                continue
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))
    return "".join(text_parts)


# ── task parsing ──────────────────────────────────────────────────────────────

def _parse_tasks(orchestrator_response: str) -> dict[str, str]:
    """Extract [TAREFA para X]: ... blocks from orchestrator response."""
    tasks: dict[str, str] = {}
    for match in _TASK_RE.finditer(orchestrator_response):
        name = match.group(1).strip().lower()
        description = match.group(2).strip()
        if name in _SPECIALIST_NAMES:
            tasks[name] = description
    return tasks


# ── coordinator loop ──────────────────────────────────────────────────────────

async def _run(prompt: str | None, proxy_root_url: str, base_token: str) -> None:
    try:
        import httpx
    except ImportError:
        print("Erro: instale httpx com: uv pip install httpx", file=sys.stderr)
        raise SystemExit(1)

    async with httpx.AsyncClient() as client:
        orch_history: list[dict] = []

        async def _ask_orchestrator(user_msg: str) -> str:
            orch_history.append({"role": "user", "content": user_msg})
            reply = await _chat(client, proxy_root_url, base_token, "orquestrador", orch_history)
            orch_history.append({"role": "assistant", "content": reply})
            return reply

        if prompt:
            # Single-shot mode
            await _single_turn(client, proxy_root_url, base_token, prompt, _ask_orchestrator)
        else:
            # Interactive loop
            print("\033[1;96m=== ll-auto: Multi-Agente Automático ===\033[0m")
            print("Digite sua mensagem (Ctrl+C para sair)\n")
            while True:
                try:
                    user_input = input("\033[1;32mVocê: \033[0m").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nAté mais!")
                    break
                if not user_input:
                    continue
                await _single_turn(client, proxy_root_url, base_token, user_input, _ask_orchestrator)
                print()


async def _single_turn(
    client: Any,
    proxy_root_url: str,
    base_token: str,
    user_input: str,
    ask_orchestrator: Any,
) -> None:
    """Process one user message through the full multi-agent pipeline."""

    # Step 1: Orchestrator plans
    print("\033[90m[Orquestrador pensando...]\033[0m")
    orch_response = await ask_orchestrator(user_input)

    # Step 2: Parse task delegations
    tasks = _parse_tasks(orch_response)

    if not tasks:
        # No delegation — orchestrator answered directly
        print(f"\033[1;96mOrquestrador:\033[0m {orch_response}")
        return

    # Show orchestrator's plan
    print(f"\033[1;96mOrquestrador (plano):\033[0m {orch_response}\n")
    print(f"\033[90m[Delegando para {len(tasks)} especialista(s): {', '.join(tasks.keys())}...]\033[0m")

    # Step 3: Run specialists in parallel
    async def _run_specialist(role: str, task: str) -> tuple[str, str]:
        print(f"\033[90m  [{role.capitalize()} executando...]\033[0m")
        messages = [{"role": "user", "content": task}]
        try:
            result = await _chat(client, proxy_root_url, base_token, role, messages)
        except Exception as exc:
            result = f"[Erro: {exc}]"
        print(f"\033[90m  [{role.capitalize()} concluído]\033[0m")
        return role, result

    results = await asyncio.gather(*[_run_specialist(r, t) for r, t in tasks.items()])

    # Step 4: Send results back to orchestrator for consolidation
    results_text = "\n\n".join(
        f"=== {role.capitalize()} ===\n{result}" for role, result in results
    )
    consolidation_prompt = (
        f"Os especialistas concluíram suas tarefas. Resultados:\n\n{results_text}\n\n"
        f"Consolide os resultados e apresente a resposta final ao usuário."
    )
    print("\033[90m[Orquestrador consolidando resultados...]\033[0m")
    final = await ask_orchestrator(consolidation_prompt)
    print(f"\n\033[1;96mResposta Final:\033[0m\n{final}")


# ── entry point ───────────────────────────────────────────────────────────────

def launch(argv: list[str] | None = None) -> None:
    """ll-auto entry point."""
    args = list(sys.argv[1:] if argv is None else argv)

    prompt: str | None = None
    if "-p" in args:
        idx = args.index("-p")
        if idx + 1 < len(args):
            prompt = args[idx + 1]
        else:
            print("Erro: -p requer um argumento.", file=sys.stderr)
            raise SystemExit(1)

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)

    if error := preflight_proxy(proxy_root_url):
        print(f"Proxy não está rodando em {proxy_root_url}: {error}", file=sys.stderr)
        print("Inicie em outro terminal com: ll-server", file=sys.stderr)
        raise SystemExit(1)

    base_token = claude_auth_token(settings.anthropic_auth_token)

    try:
        asyncio.run(_run(prompt, proxy_root_url, base_token))
    except KeyboardInterrupt:
        pass
