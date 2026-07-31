# -*- coding: utf-8 -*-
import os
from crewai import Agent, Crew, Process, Task, LLM

# Anthropic API Key aus den Umgebungsvariablen prÃ¼fen
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

if not ANTHROPIC_KEY:
    print("[Error] ANTHROPIC_API_KEY fehlt in den Railway Environment Variables!")

# Anthropic Claude 3.5 Sonnet als dediziertes LLM definieren
claude_llm = LLM(
    model="anthropic/claude-3-5-sonnet-latest",
    api_key=ANTHROPIC_KEY
)

# Agents mit dem Claude-LLM konfigurieren
devops_agent = Agent(
    role='Lead DevOps Engine',
    goal='Maintain the Exchange API Sentinel infrastructure and verify deployment status',
    backstory='Senior Site Reliability Engineer specializing in zero-touch cloud operations.',
    llm=claude_llm,
    verbose=True
)

marketing_agent = Agent(
    role='Growth Engine',
    goal='Monitor and execute automated dispatches to drive traffic to the landing page',
    backstory='Technical marketer focused on quant trader acquisition.',
    llm=claude_llm,
    verbose=True
)

ceo_agent = Agent(
    role='Autonomous CEO',
    goal='Evaluate validation threshold (5 conversions in 96h) and trigger backend deployment if passed',
    backstory='Data-driven SaaS CEO focused strictly on unit economics and zero-human operations.',
    llm=claude_llm,
    verbose=True
)

# Tasks definieren
task_check_status = Task(
    description='Verify that https://evolution5s.github.io/api-sentinel/ is live and responding with HTTP 200.',
    agent=devops_agent,
    expected_output='Status report of the live landing page.'
)

task_eval_conversions = Task(
    description='Check conversion metrics. If >= 5 signups detected within 96 hours, trigger Hetzner deployment sequence.',
    agent=ceo_agent,
    expected_output='Go/No-Go Decision Report'
)

# Crew instanziieren
crew = Crew(
    agents=[devops_agent, marketing_agent, ceo_agent],
    tasks=[task_check_status, task_eval_conversions],
    process=Process.sequential
)

if __name__ == "__main__":
    print("[Railway Worker] OpenCrew Autonomous Loop Started (Anthropic Claude)...")
    crew.kickoff()
    print("[Railway Worker] Execution finished.")


