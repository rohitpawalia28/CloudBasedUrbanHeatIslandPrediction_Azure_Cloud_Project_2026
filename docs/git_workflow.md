# Solo Git Workflow

The course guideline asks students not to commit directly to `main`. Although this is an individual project, the same quality workflow will be followed.

```text
feature/rohit-kumar  ->  develop  ->  main
```

| Branch | Purpose |
|---|---|
| `main` | Stable, review-ready version only. |
| `develop` | Integration branch for planned work. |
| `feature/rohit-kumar` | Rohit Kumar's active working branch. |

## Suggested commands after creating the GitHub repository

```bash
git clone <repository-url>
git checkout -b develop
git push -u origin develop
git checkout -b feature/rohit-kumar
git push -u origin feature/rohit-kumar
```

Commit meaningful updates to `feature/rohit-kumar`, open a pull request to `develop`, review the changes yourself using the PR diff, then merge. When `develop` is tested and review-ready, open a pull request from `develop` to `main`.

Suggested Phase I commit messages:

```text
docs: add project overview and objectives
docs: add individual 15-paper literature survey
docs: add research gap analysis and novelty summary
docs: add AWS architecture diagrams and service plan
docs: add dataset details and repository placeholders
```
