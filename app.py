"""COM Allocation Planner — Streamlit demo app.

Run locally:   streamlit run app.py
Demo logins:   planner / planner123     approver / approver123
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

import allocation as eng
import auth
import store

st.set_page_config(page_title="COM Allocation Planner", page_icon="📦", layout="wide")

STATUS_COLOR = {"Draft": "gray", "Submitted": "orange", "Approved": "green", "Rejected": "red"}


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def login_page():
    users, is_demo = auth.load_users(st.secrets if _has_secrets() else None)
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown("## COM Allocation Planner")
        st.caption("Monthly warehouse allocation · sign in to continue")
        role = st.radio("Sign in as", ["Planner", "Approver"], horizontal=True)
        with st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            ok = st.form_submit_button("Sign in", type="primary", width="stretch")
        if ok:
            user = auth.check_login(users, username, password)
            if not user:
                st.error("Wrong username or password.")
            elif user["role"] != role.lower():
                st.error(f"This account is not a {role.lower()}. Pick “{user['role'].title()}” above.")
            else:
                st.session_state.user = user
                st.rerun()
        with st.container(border=True):
            if role == "Planner":
                st.markdown("**Planner** — set up the monthly run, run the allocation, check warnings "
                            "and submit the plan for approval.")
            else:
                st.markdown("**Approver** — review submitted plans (cost, space, warnings) and approve "
                            "or send them back with a comment.")
        if is_demo:
            st.info("Demo accounts: **planner / planner123** and **approver / approver123**. "
                    "All data in this app is sample data.")


def _has_secrets() -> bool:
    try:
        return len(st.secrets) > 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Shared result view
# --------------------------------------------------------------------------- #
def status_badge(status: str) -> str:
    return f":{STATUS_COLOR.get(status, 'gray')}[**{status}**]"


def render_results(solver_status, summary, alloc, capacity, warnings):
    if solver_status == "Optimal":
        st.success("Best plan found (optimal).")
    elif solver_status == "Stopped at time limit":
        st.warning("Solver stopped at the time limit — plan is usable but may not be the best.")
    else:
        st.error(f"No plan: {solver_status}. Check limits and space.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total shipping cost", f"${summary['total_cost']:,.0f}")
    c2.metric("Units placed", f"{summary['total_units']:,.0f}")
    tot = summary["total_units"] or 1
    c3.metric("Optimizer / rules", f"{summary['optimizer_units'] / tot:.0%} / {summary['rule_units'] / tot:.0%}")
    c4.metric("Overflow units", f"{summary['overflow_units']:,.0f}",
              delta="over space" if summary["overflow_units"] > 0 else None, delta_color="inverse")

    tab1, tab2, tab3, tab4 = st.tabs(["By warehouse", f"Warnings ({len(warnings)})", "Product detail", "All rows"])
    with tab1:
        left, right = st.columns([3, 2])
        with left:
            util = eng.utilisation(alloc, capacity)
            st.dataframe(util, hide_index=True, width="stretch",
                         column_config={"Space used %": st.column_config.ProgressColumn(
                             "Space used %", min_value=0, max_value=120, format="%.0f%%")})
            bw = summary["by_warehouse"].copy()
            bw = bw.rename(columns={"units": "Units", "cost": "Cost ($)", "share_pct": "Share %"}).round(1)
            st.dataframe(bw, width="stretch")
        with right:
            chart = alloc.pivot_table(index="warehouse", columns="storage", values="units", aggfunc="sum").fillna(0)
            st.caption("Units by warehouse and storage type")
            st.bar_chart(chart)
    with tab2:
        if not warnings:
            st.write("No warnings.")
        for w in warnings:
            if w["level"] in ("high", "error"):
                st.warning(w["message"])
            else:
                st.info(w["message"])
    with tab3:
        pt = st.selectbox("Product type", sorted(alloc["product_type"].unique()))
        sub = alloc[alloc["product_type"] == pt]
        st.write(f"Placed by: **{', '.join(sub['method'].unique())}** · storage **{sub['storage'].iloc[0]}**")
        piv = sub.pivot_table(index="zone", columns="warehouse", values="units", aggfunc="sum").fillna(0)
        st.dataframe(piv, width="stretch")
    with tab4:
        st.dataframe(alloc.round(2), hide_index=True, width="stretch")
        st.download_button("Download plan (CSV)", alloc.to_csv(index=False).encode(),
                           file_name="allocation_plan.csv", mime="text/csv")


def render_saved_run(run):
    st.markdown(f"**Run #{run['id']}** · {run['month']} · by {run['created_by']} · {run['created_at']} · "
                f"{status_badge(run['status'])}")
    if run.get("comment"):
        st.markdown(f"> **{run['decided_by']}:** {run['comment']}")
    render_results(run["solver_status"], run["summary"], run["allocation"], run["capacity"], run["warnings"])


# --------------------------------------------------------------------------- #
# Planner pages
# --------------------------------------------------------------------------- #
def _init_inputs():
    if "inputs" not in st.session_state:
        st.session_state.inputs = {
            "products": eng.sample_products(), "costs": eng.sample_costs(),
            "capacity": eng.sample_capacity(), "limits": eng.sample_limits(),
            "restrictions": eng.sample_restrictions(),
        }


def page_new_run(user):
    _init_inputs()
    inp = st.session_state.inputs
    st.title("New allocation run")
    st.caption("Set the month and limits, check the warnings, then run. Sample data is pre-filled — edit any table.")

    months = [(date.today().replace(day=1) + pd.DateOffset(months=i)).strftime("%B %Y") for i in range(1, 7)]
    c1, c2, c3 = st.columns(3)
    month = c1.selectbox("Target month", months)
    penalty = c2.number_input("Overflow penalty ($ per unit over space)", 1.0, 1000.0, 50.0, 5.0)
    time_limit = c3.number_input("Solver time limit (seconds)", 5, 600, 60, 5)

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Warehouse share limits")
        limits = st.data_editor(inp["limits"], width="stretch", key="ed_limits",
                                column_config={"min_pct": st.column_config.NumberColumn("Min %", min_value=0, max_value=100),
                                               "max_pct": st.column_config.NumberColumn("Max %", min_value=0, max_value=100)})
        st.subheader("Space by storage type (units)")
        capacity = st.data_editor(inp["capacity"], width="stretch", key="ed_cap")
        with st.expander("Demand by product and zone"):
            products = st.data_editor(inp["products"], width="stretch", hide_index=True, key="ed_prod",
                                      disabled=["product_type", "storage", "velocity"])
        with st.expander("Shipping cost per unit (warehouse → zone)"):
            costs = st.data_editor(inp["costs"], width="stretch", key="ed_cost")
            st.caption("Empty cell = missing lane. It will be filled with the warehouse average and flagged.")
        with st.expander("Business rules (product forbidden in warehouse)"):
            restrictions = st.data_editor(inp["restrictions"], width="stretch", hide_index=True,
                                          num_rows="dynamic", key="ed_rules",
                                          column_config={"warehouse": st.column_config.SelectboxColumn(
                                              "warehouse", options=eng.WAREHOUSES),
                                              "rule": st.column_config.SelectboxColumn("rule", options=["Forbidden"])})

    checks = eng.pre_run_checks(products, costs, capacity, limits)
    with right:
        st.subheader("Checks before running")
        icon = {"ok": "✅", "error": "❌", "warning": "⚠️", "info": "ℹ️"}
        with st.container(border=True):
            for c in checks:
                st.write(f"{icon[c['level']]} {c['message']}")
        blocked = eng.has_errors(checks)
        run = st.button("Run allocation", type="primary", width="stretch", disabled=blocked)
        if blocked:
            st.caption("Fix the ❌ items to enable the run.")

    if run:
        with st.spinner("Placing special products, building the model and solving…"):
            res = eng.run_allocation(products, costs, capacity, limits, restrictions.dropna(how="all"),
                                     penalty=penalty, time_limit=time_limit)
        run_id = store.save_run(month, user["username"], res, capacity,
                                {"penalty": penalty, "time_limit": time_limit,
                                 "limits": limits.reset_index().to_dict("records")})
        st.session_state.last_run_id = run_id
        st.toast(f"Run #{run_id} saved as draft")

    run_id = st.session_state.get("last_run_id")
    if run_id:
        run_data = store.get_run(run_id)
        if run_data:
            st.divider()
            st.header(f"Result — run #{run_id}")
            render_saved_run(run_data)
            if run_data["status"] in ("Draft", "Rejected") and run_data["solver_status"] in (
                    "Optimal", "Stopped at time limit"):
                if st.button("Submit for approval", type="primary"):
                    store.submit(run_id)
                    st.toast("Submitted to approver")
                    st.rerun()


def page_my_runs(user):
    st.title("My runs")
    runs = store.list_runs(created_by=user["username"])
    if runs.empty:
        st.info("No runs yet. Start one from **New run**.")
        return
    st.dataframe(runs, hide_index=True, width="stretch")
    run_id = st.selectbox("Open run", runs["id"].tolist(), format_func=lambda i: f"#{i}")
    run = store.get_run(run_id)
    if run:
        render_saved_run(run)
        if run["status"] in ("Draft", "Rejected") and run["solver_status"] in ("Optimal", "Stopped at time limit"):
            if st.button("Submit for approval", type="primary", key="submit_my"):
                store.submit(run_id)
                st.rerun()


# --------------------------------------------------------------------------- #
# Approver pages
# --------------------------------------------------------------------------- #
def page_approvals(user):
    st.title("Plans waiting for approval")
    pending = store.list_runs(status="Submitted")
    if pending.empty:
        st.success("Nothing to approve right now.")
        return
    st.dataframe(pending[["id", "month", "created_by", "created_at", "solver_status"]],
                 hide_index=True, width="stretch")
    run_id = st.selectbox("Review run", pending["id"].tolist(), format_func=lambda i: f"#{i}")
    run = store.get_run(run_id)
    render_saved_run(run)
    st.divider()
    st.subheader("Decision")
    comment = st.text_area("Comment (required to reject)", key=f"comment_{run_id}")
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Approve plan", type="primary", width="stretch"):
        store.decide(run_id, user["username"], True, comment)
        st.toast(f"Run #{run_id} approved")
        st.rerun()
    if c2.button("Send back", width="stretch"):
        if not comment.strip():
            st.error("Add a comment so the planner knows what to fix.")
        else:
            store.decide(run_id, user["username"], False, comment)
            st.toast(f"Run #{run_id} sent back")
            st.rerun()


def page_history(user):
    st.title("All runs")
    runs = store.list_runs()
    if runs.empty:
        st.info("No runs yet.")
        return
    st.dataframe(runs, hide_index=True, width="stretch")
    run_id = st.selectbox("Open run", runs["id"].tolist(), format_func=lambda i: f"#{i}")
    render_saved_run(store.get_run(run_id))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    user = st.session_state.get("user")
    if not user:
        login_page()
        return

    pages = ({"New run": page_new_run, "My runs": page_my_runs} if user["role"] == "planner"
             else {"Approvals": page_approvals, "All runs": page_history})
    with st.sidebar:
        st.markdown(f"### {user['name']}")
        st.caption(f"Role: **{user['role'].title()}**")
        choice = st.radio("Go to", list(pages), label_visibility="collapsed")
        if user["role"] == "approver":
            n = len(store.list_runs(status="Submitted"))
            st.caption(f"{n} plan(s) waiting")
        st.divider()
        if st.button("Sign out", width="stretch"):
            st.session_state.clear()
            st.rerun()
        st.caption("Demo app · sample data · simplified engine (SciPy HiGHS instead of Gurobi)")
    pages[choice](user)


main()
