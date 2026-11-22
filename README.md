# gitlab-ai-code-review
Automated GitLab MR code review agent —structured JSON findings, diff line validation, and zero-noise inline comments.

pip install -r requirements.txt
python review_agent.py --project group/repo --mr 123            # dry run
python review_agent.py --project group/repo --mr 123 --post     # post inline comments
