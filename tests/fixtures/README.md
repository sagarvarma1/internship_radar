Trimmed copies of real public API responses captured on 2026-11-04:

- ashby_notion.json: api.ashbyhq.com/posting-api/job-board/notion
- greenhouse_figma.json: boards-api.greenhouse.io/v1/boards/figma/jobs?content=true
- lever_palantir.json: api.lever.co/v0/postings/palantir?mode=json

Each keeps 5 jobs (internships first) with long text fields cut to 1500 chars. Structure is unchanged.
- workday_{nvidia,salesforce,adobe}_jobs.json: POST /wday/cxs/{tenant}/{site}/jobs (empty search), first 3 postings + full facets
- workday_nvidia_detail.json: GET job detail, description cut to 1500 chars
