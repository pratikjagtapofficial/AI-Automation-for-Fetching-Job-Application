import sys, yaml
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from src.validator import validate, check_forbidden_skills, check_experience_claims

prof = yaml.safe_load(Path("config/profile.yml").read_text(encoding="utf-8"))

cases = [
 ("clean tailored bullet",
  "Built RAG pipelines with LangChain and FastAPI, deployed on Vultr.", True),
 ("sneaks in Docker",
  "Containerised services with Docker and deployed to production.", False),
 ("sneaks in Kubernetes",
  "Orchestrated microservices using Kubernetes at scale.", False),
 ("sneaks in AWS",
  "Deployed LLM services on AWS Lambda with S3 storage.", False),
 ("sneaks in CrewAI",
  "Built multi-agent systems using CrewAI and AutoGen.", False),
 ("inflates experience",
  "AI Engineer with 5+ years of experience building LLM systems.", False),
 ("honest experience",
  "AI Engineer with 1 year of experience building LLM systems.", True),
 ("LangGraph allowed (shallow)",
  "Orchestrated agent flows with LangGraph and LangChain.", True),
 ("cloud PJ actually has",
  "Deployed on DigitalOcean and Vultr with Cloudflare CDN.", True),
 ("substring trap: 'GCP' inside word",
  "Worked on ABCGCPX internal tooling.", True),
 ("Docker with punctuation",
  "Familiar with Docker, Kubernetes.", False),
]

fails = 0
for name, text, want_ok in cases:
    v = validate(text, prof)
    got = v.ok
    mark = "PASS" if got == want_ok else "FAIL"
    if got != want_ok: fails += 1
    print(f"  {mark}  {name:<34} ok={got}")
    for x in v.violations: print(f"          BLOCKED: {x}")
    for w in v.warnings:   print(f"          warn:    {w}")
print(f"\n{len(cases)-fails}/{len(cases)} passed" + ("" if fails else "  — ALL PASS"))
