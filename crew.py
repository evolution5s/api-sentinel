import os
import time
from crewai import Agent, Crew, Process, Task

# Umgebungsvariablen prüfen
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not OPENAI_KEY:
    print("[Error] OPENAI_API_KEY fehlt in den Railway Environment Variables!")

# Agents definieren
devops_agent = Agent(
    role='Lead DevOps Engine',
    goal='Maintain the Exchange API Sentinel infrastructure and verify deployment status',
    backstory='Senior Site Reliability Engineer specializing in zero-touch cloud operations.',
    verbose=True
)

marketing_agent = Agent(
    role='Growth Engine',
    goal='Monitor and execute automated dispatches to drive traffic to the landing page',
    backstory='Technical marketer focused on quant trader acquisition.',
    verbose=True
)

ceo_agent = Agent(
    role='Autonomous CEO',
    goal='Evaluate validation threshold (5 conversions in 96h) and trigger backend deployment if passed',
    backstory='Data-driven SaaS CEO focused strictly on unit economics and zero-human operations.',
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

# Crew konfigurieren
crew = Crew(
    agents=[devops_agent, marketing_agent, ceo_agent],
    tasks=[task_check_status, task_eval_conversions],
    process=Process.sequential
)

if __name__ == "__main__":
    print("[Railway Worker] OpenCrew Autonomous Loop Started...")
    crew.kickoff()
    print("[Railway Worker] Execution finished. Sleeping until next cycle...")
