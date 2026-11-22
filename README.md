# GitLab MR Reviewer
An agent that pulls a GitLab Merge Request, has a frontier model review the diff, and optionally posts the findings as inline comments on the MR.
By default it does a dry run and prints the findings without posting anything.

## How it works

1. **Fetch** the MR metadata (`diff_refs`) and per-file diffs through the GitLab REST API.
2. **Annotate** each diff line so the model never has to count line numbers:
   - Added lines become `[L<new_line>] +...`.
   - Context lines become `[L<new_line>] ...`.
   - Removed lines become `[DEL] -...`.
3. **Review**: send the annotated diff to the model with a structured-output schema. The response is a validated list of comments.
4. **Validate**: drop any comment whose `new_path` or `line` isn't an added or context line in the diff. This guards against hallucinated locations.
5. **Output**: print the comments (dry run), or with `--post`, create inline discussions on the MR.

Example of an annotated diff:

```
@@ -10,4 +10,5 @@
[L10]  def get_user(id):
[DEL] -    return db.query(id)
[L11] +    user = db.query(id)
[L12] +    return user.name
```

Example of a model response:

```json
{
  "summary": "Adds user lookup; missing None handling.",
  "comments": [
    {
      "new_path": "app/users.py",
      "line": 12,
      "severity": "high",
      "comment": "db.query may return None, so user.name raises AttributeError."
    }
  ]
}
```

## What it reports

The system prompt tells the model to act as a senior staff engineer and report only high-signal issues:

- Bugs and logic or edge-case flaws
- Race conditions
- Security vulnerabilities
- Breaking changes
- Missing error handling

It skips formatting and style nitpicks. If it finds no clear issues, it returns an empty `comments` list.

## Requirements

- Python 3.10+
- A GitLab personal access token with the `api` scope
- A Gemini API key

## Setup

```bash
cd gitlab-mr-reviewer
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
copy .env.example .env          # then edit .env
```

### Configuration (`.env`)

| Variable | Description | Example |
|---|---|---|
| `GITLAB_URL` | Base URL of your GitLab instance | `https://gitlab.com` |
| `GITLAB_TOKEN` | Personal access token (`api` scope) | `glpat-...` |
| `GEMINI_API_KEY` | Gemini API key | `...` |
| `MODEL` | Model name (optional) | `gemini-2.5-pro` |

Never commit `.env`. Add it to `.gitignore`.

## Usage

```bash
# Dry run: prints findings and posts nothing
python review_agent.py --project group/repo --mr 123

# Post findings as inline comments on the MR
python review_agent.py --project group/repo --mr 123 --post
```

| Argument | Description |
|---|---|
| `--project` | Project path (`group/subgroup/repo`) or numeric ID |
| `--mr` | MR IID, the number shown in the MR URL |
| `--post` | Post comments to GitLab. Without it, results are only printed. |

### Sample dry-run output

```
Summary: Adds user lookup and refunds. Both lack error handling.

app/users.py:12 [high] db.query may return None, so user.name raises AttributeError.

app/payments.py:44 [critical] Read-modify-write on order.balance without a lock...
```

## Behaviour details

- **Skipped files:** deleted files, files GitLab marks `too_large` or `collapsed`, and files with no text diff (such as images).
- **Diff budget:** total diff text is capped at 400,000 characters (`MAX_DIFF_CHARS`). Files over the budget are skipped, with a warning on stderr.
- **Validation:** comments pointing at a file or line that isn't in the diff are dropped, and a warning is printed with the count.
- **Fallback posting:** if GitLab rejects an inline position, for example because the SHAs went stale after a force-push, the comment is posted as a general MR note.
- **Clean MRs:** with `--post` and no findings, a note saying no high-signal issues were found is posted.

## Project structure

```
gitlab-mr-reviewer/
├── review_agent.py      # fetch, annotate, review, validate, post
├── requirements.txt
├── .env.example
└── README.md
```

## Limitations and ideas

- **Diff-only context:** the model sees only the diff. Fetching full file contents at `head_sha` would reduce false positives.
- **Large MRs:** files over the budget are skipped. Chunking by file and merging the results would give better coverage.
- **Duplicate comments:** re-running with `--post` posts comments again. You could fetch existing discussions and skip matches.
- **Other providers:** only `call_model()` is provider-specific. Swap it to use OpenAI, Anthropic or another model.
- **Automation:** run it from a GitLab CI job on merge request events, or from a webhook.

## Security notes

- Use a token with the minimum scope needed, and prefer a dedicated bot account for `--post`.
- The diff is sent to an external model provider. Confirm that this is allowed for the repositories you review.
- Run Snyk code and SCA scans on this tool, and check `requests`, `pydantic`, `google-genai` and `python-dotenv` with a package health check before adopting it.
