import argparse, os, re, sys
import requests
from dotenv import load_dotenv
from pydantic import BaseModel
from google import genai
from google.genai import types

load_dotenv()

SYSTEM_INSTRUCTION = (
    "You are a senior staff engineer reviewing a GitLab Merge Request.\n"
    "Rules:\n"
    "1. Report high-signal issues only: bugs, logic/edge-case flaws, race conditions, "
    "security vulnerabilities, breaking changes, and missing error handling.\n"
    "2. Skip formatting/style nitpicks handled by linters or formatters.\n"
    "3. Use exact `new_path` and `[L<num>]` line numbers shown in the diff. Never target `[DEL]` lines.\n"
    "4. If no clear issues are found, return an empty comments list."
)

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MAX_DIFF_CHARS = 400_000


class ReviewComment(BaseModel):
    new_path: str
    line: int
    severity: str  # critical | high | medium
    comment: str


class Review(BaseModel):
    summary: str
    comments: list[ReviewComment]


class GitLab:
    def __init__(self, url: str, token: str):
        self.base = f"{url.rstrip('/')}/api/v4"
        self.s = requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = token

    def _req(self, method, path, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=60, **kw)
        r.raise_for_status()
        return r.json()

    def get_mr(self, project, iid):
        return self._req("GET", f"/projects/{requests.utils.quote(project, safe='')}/merge_requests/{iid}")

    def get_diffs(self, project, iid):
        out, page = [], 1
        while True:
            batch = self._req("GET", f"/projects/{requests.utils.quote(project, safe='')}/merge_requests/{iid}/diffs",
                              params={"per_page": 100, "page": page})
            out += batch
            if len(batch) < 100:
                return out
            page += 1

    def post_inline(self, project, iid, refs, c: ReviewComment, old_path):
        body = {
            "body": f"**[{c.severity.upper()}]** {c.comment}",
            "position[position_type]": "text",
            "position[base_sha]": refs["base_sha"],
            "position[start_sha]": refs["start_sha"],
            "position[head_sha]": refs["head_sha"],
            "position[old_path]": old_path,
            "position[new_path]": c.new_path,
            "position[new_line]": c.line,
        }
        return self._req("POST", f"/projects/{requests.utils.quote(project, safe='')}/merge_requests/{iid}/discussions", data=body)

    def post_note(self, project, iid, text):
        return self._req("POST", f"/projects/{requests.utils.quote(project, safe='')}/merge_requests/{iid}/notes", data={"body": text})


def annotate(diff: str):
    """Return (annotated_text, set_of_commentable_new_lines)."""
    lines, valid, old, new = [], set(), 0, 0
    for raw in diff.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            old, new = int(m[1]), int(m[2])
            lines.append(raw)
        elif raw.startswith("+"):
            lines.append(f"[L{new}] {raw}"); valid.add(new); new += 1
        elif raw.startswith("-"):
            lines.append(f"[DEL] {raw}"); old += 1
        elif raw.startswith("\\"):
            continue
        else:
            lines.append(f"[L{new}] {raw}"); valid.add(new); old += 1; new += 1
    return "\n".join(lines), valid


def build_prompt(mr, diffs):
    parts, valid_map, old_paths, size = [], {}, {}, 0
    for d in diffs:
        if d.get("deleted_file") or d.get("too_large") or d.get("collapsed") or not d.get("diff"):
            continue
        text, valid = annotate(d["diff"])
        block = f"### new_path: {d['new_path']}\n{text}\n"
        if size + len(block) > MAX_DIFF_CHARS:
            print(f"warn: skipping {d['new_path']} (diff budget exceeded)", file=sys.stderr)
            continue
        size += len(block)
        parts.append(block)
        valid_map[d["new_path"]] = valid
        old_paths[d["new_path"]] = d["old_path"]
    header = f"MR title: {mr['title']}\nDescription: {mr.get('description') or ''}\n\n"
    return header + "\n".join(parts), valid_map, old_paths


def call_model(prompt: str) -> Review:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=os.getenv("MODEL", "gemini-2.5-pro"),
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=Review,
            temperature=0.1,
        ),
    )
    return resp.parsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="e.g. group/subgroup/repo or numeric id")
    ap.add_argument("--mr", type=int, required=True, help="MR iid")
    ap.add_argument("--post", action="store_true", help="post comments to GitLab (default: dry run)")
    a = ap.parse_args()

    gl = GitLab(os.environ["GITLAB_URL"], os.environ["GITLAB_TOKEN"])
    mr = gl.get_mr(a.project, a.mr)
    diffs = gl.get_diffs(a.project, a.mr)
    prompt, valid_map, old_paths = build_prompt(mr, diffs)
    if not valid_map:
        print("No reviewable changes."); return

    review = call_model(prompt)
    good = [c for c in review.comments if c.line in valid_map.get(c.new_path, set())]
    dropped = len(review.comments) - len(good)
    if dropped:
        print(f"warn: dropped {dropped} comment(s) with invalid path/line", file=sys.stderr)

    print(f"Summary: {review.summary}\n")
    for c in good:
        print(f"{c.new_path}:{c.line} [{c.severity}] {c.comment}\n")

    if a.post:
        refs = mr["diff_refs"]
        for c in good:
            try:
                gl.post_inline(a.project, a.mr, refs, c, old_paths[c.new_path])
            except requests.HTTPError as e:
                gl.post_note(a.project, a.mr, f"`{c.new_path}:{c.line}` **[{c.severity.upper()}]** {c.comment}")
                print(f"inline post failed ({e}); posted as general note", file=sys.stderr)
        if not good:
            gl.post_note(a.project, a.mr, "Automated review: no high-signal issues found.")


if __name__ == "__main__":
    main()
