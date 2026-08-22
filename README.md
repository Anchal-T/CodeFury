# Git Branching & Commit Guide

## Branch Structure

- `main` → Stable, production-ready code only.
- `dev` → Active development and integration branch.
- `feature/*`, `fix/*`, etc. → Individual work branches created from `dev`.

## Workflow

1. Create your branch from `dev`:

```bash
git checkout dev
git pull origin dev
git checkout -b feature/your-feature
```

2. Make your changes and commit:

```bash
git add .
git commit -m "Add: your change"
```

3. Push your branch:

```bash
git push -u origin feature/your-feature
```

4. Create a Pull Request from your branch → `dev`.

5. After testing and review, merge into `dev`.

6. **Only stable, tested, working versions are merged from `dev` → `main`.**

### Important

- Never commit directly to `main`.
- Never create feature branches from `main`.
- Always create new branches from the latest `dev`.
- Keep commits small and descriptive.
- Do not merge unfinished or untested code into `main`.
