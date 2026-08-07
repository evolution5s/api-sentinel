"""Narrow, defensive patches for confirmed crewai bugs that break this repo
on Claude Sonnet 5 (and the wider Claude 4.6+ model family). Each patch only
touches what it names explicitly and is applied defensively - if crewai's
internals ever change shape, apply_patches() just skips that one patch and
prints a warning instead of crashing the whole app on import.
"""


def _patch_max_iterations_final_answer_role() -> bool:
    """Root cause (verified directly in crewai.utilities.agent_utils, both
    the installed 1.15.9 and the pinned 1.15.11): when an agent reaches
    max_iter, crewai tries to force a final answer by appending the forcing
    instruction as an ASSISTANT-role message and sending that as the LAST
    message in the conversation. Every current Claude model rejects that
    with a 400 ("This model does not support assistant message prefill.
    The conversation must end with a user message.") - so reaching max_iter
    doesn't gracefully truncate the task, it crashes it, and the exception
    propagates all the way up through crew.kickoff() and takes the rest of
    the cron cycle down with it. Reproduced in production.

    Same behavior, minimal fix: append the forcing instruction as a
    user-role message instead - the one change needed to make the request
    valid on every current Claude model. Not an upstream fix (that belongs
    in crewai itself, worth reporting), just enough to stop max_iter from
    being load-bearing-fatal here.
    """
    try:
        from crewai.agents.parser import AgentFinish
        from crewai.utilities import agent_utils
        from crewai.utilities.agent_utils import format_answer, format_message_for_llm
    except ImportError as exc:
        print(f"[crewai_patches] could not import max_iter patch targets: {exc}")
        return False

    def _fixed_handle_max_iterations_exceeded(formatted_answer, printer, messages, llm, callbacks, verbose=True):
        if verbose:
            printer.print(
                content="Maximum iterations reached. Requesting final answer.",
                color="yellow",
            )

        if formatted_answer and hasattr(formatted_answer, "text"):
            forcing_message = (
                formatted_answer.text + f"\n{agent_utils.I18N_DEFAULT.errors('force_final_answer')}"
            )
        else:
            forcing_message = agent_utils.I18N_DEFAULT.errors("force_final_answer")

        # The one substantive change vs. crewai's own implementation:
        # "user" instead of "assistant" - see the docstring above.
        messages.append(format_message_for_llm(forcing_message, role="user"))

        answer = llm.call(messages, callbacks=callbacks)
        if answer is None or answer == "":
            raise ValueError("Invalid response from LLM call - None or empty.")

        formatted = format_answer(answer=answer)
        if isinstance(formatted, AgentFinish):
            return formatted
        return AgentFinish(thought=formatted.thought, output=formatted.text, text=formatted.text)

    patched_modules = []
    for module_path in ("crewai.agents.crew_agent_executor", "crewai.experimental.agent_executor"):
        try:
            module = __import__(module_path, fromlist=["handle_max_iterations_exceeded"])
        except ImportError:
            continue
        if not hasattr(module, "handle_max_iterations_exceeded"):
            continue
        module.handle_max_iterations_exceeded = _fixed_handle_max_iterations_exceeded
        patched_modules.append(module_path)

    if patched_modules:
        print(f"[crewai_patches] patched handle_max_iterations_exceeded in: {patched_modules}")
    else:
        print(
            "[crewai_patches] WARNING: handle_max_iterations_exceeded not found in any "
            "expected module - patch not applied, max_iter crashes may recur"
        )
    return bool(patched_modules)


def _patch_disable_strict_tool_schemas() -> bool:
    """Root cause (verified directly in crewai.utilities.agent_utils, both
    the installed 1.15.9 and the pinned 1.15.11): convert_tools_to_openai_
    schema() unconditionally bakes "strict": True into every tool's schema,
    with no per-tool or per-agent way to opt out. Anthropic's native tool-use
    API enforces a hard cap of 20 "strict" tools per request and rejects the
    whole call with a 400 the moment a single agent has 21+ tools ("Too many
    strict tools (21). The maximum number of strict tools supported is 20.")
    - not a rare edge case, a threshold this repo's own agents were always
    going to cross as more tools get added over time. Reproduced in
    production: ceo_agent crossed 20 tools and every task assigned to it
    failed crew.kickoff() outright.

    Same behavior otherwise, minimal fix: after crewai builds each tool's
    OpenAI-format schema, strip the "strict" flag it hardcodes so Anthropic
    never counts these tools against its strict-tool cap. Every tool in this
    repo already validates its own arguments and returns a JSON error
    instead of crashing on bad input (see tools.py/holding.py), so turning
    off provider-side strict-schema enforcement removes no real safety net
    here - it only removes an artificial ceiling on how many tools one
    agent can have.
    """
    try:
        from crewai.utilities import agent_utils
    except ImportError as exc:
        print(f"[crewai_patches] could not import strict-tools patch target: {exc}")
        return False

    if not hasattr(agent_utils, "convert_tools_to_openai_schema"):
        print(
            "[crewai_patches] WARNING: convert_tools_to_openai_schema not found - "
            "strict-tools patch not applied, the 20-strict-tools 400 may recur"
        )
        return False

    original_convert = agent_utils.convert_tools_to_openai_schema

    def _convert_tools_without_strict(tools):
        openai_tools, available_functions, tool_name_mapping = original_convert(tools)
        for schema in openai_tools:
            function = schema.get("function")
            if isinstance(function, dict):
                function.pop("strict", None)
        return openai_tools, available_functions, tool_name_mapping

    patched_modules = []
    for module_path in ("crewai.utilities.agent_utils", "crewai.agents.crew_agent_executor"):
        try:
            module = __import__(module_path, fromlist=["convert_tools_to_openai_schema"])
        except ImportError:
            continue
        if not hasattr(module, "convert_tools_to_openai_schema"):
            continue
        module.convert_tools_to_openai_schema = _convert_tools_without_strict
        patched_modules.append(module_path)

    if patched_modules:
        print(f"[crewai_patches] patched convert_tools_to_openai_schema (strict disabled) in: {patched_modules}")
    else:
        print(
            "[crewai_patches] WARNING: convert_tools_to_openai_schema not patched anywhere - "
            "the 20-strict-tools 400 may recur"
        )
    return bool(patched_modules)


def apply_patches() -> None:
    """Apply every patch in this module. Called once, at import time, from
    crew.py - before any Agent/Task is constructed, since the patch has to
    be in place before an agent could ever reach its max_iter cap.
    """
    _patch_max_iterations_final_answer_role()
    _patch_disable_strict_tool_schemas()
