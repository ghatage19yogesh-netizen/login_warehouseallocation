# COM Allocation Planner (Streamlit demo)

A small web app on top of a simplified version of the COM allocation engine.

- **Login page** with two roles: **Planner** and **Approver**
- **Planner:** set month, share limits and space → pre-run checks → **Run allocation** → review plan & warnings → **Submit for approval**
- **Approver:** review submitted plans → **Approve** or **Send back** with a comment
- Runs are stored in a local SQLite file (`data/app.db`) so both roles see the same plans

> Sample data only. The engine mirrors the production logic (rule-based placement for slow sellers and
> no-demand products, medium-velocity 2-warehouse limit, business restrictions, soft capacity with penalty,
> min/max share) but uses SciPy's HiGHS solver instead of Gurobi. Do not load real company data into a public deployment.

## Files

| File | What it does |
|---|---|
| `app.py` | Streamlit UI: login, planner pages, approver pages |
| `allocation.py` | Sample data, pre-run checks, rule-based placement + LP solver, summaries |
| `auth.py` | Password hashing and login check (demo users or users from secrets) |
| `store.py` | SQLite storage of runs and approval status |
| `.streamlit/config.toml` | Theme, usage stats off |
| `.streamlit/secrets.toml.example` | Template for real user accounts |

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Demo logins: `planner / planner123` and `approver / approver123`.

## Share it on Streamlit Community Cloud

1. Create a GitHub repository (private is fine) and push this folder to it.
2. Go to **share.streamlit.io** → sign in with GitHub → **Create app** → **Deploy a public app from GitHub**.
3. Pick the repo, branch `main`, main file `app.py` → **Deploy**.
4. (Recommended) **App settings → Secrets**: paste real users in the format of `.streamlit/secrets.toml.example`.
   Create each salt/hash with `python auth.py "<password>"`. Once users are in secrets, the demo accounts stop working.
5. Share the app URL. To limit who can open it, set the app to private in **App settings → Sharing** and invite viewers by email.

Note: on Streamlit Community Cloud the SQLite file resets when the app restarts or sleeps. For a lasting
history, point `store.py` at a hosted database (e.g. BigQuery, Postgres).

## Production notes

- Replace the demo login with company SSO for real use.
- Swap `allocation.run_allocation` for the real pipeline (`run_optimization` + `AllocSolver`) behind an API.
