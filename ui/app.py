"""Streamlit front end. Runs the pipeline in-process (same code path as the API)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from analyst.core.models import Answer, ChartSpec  # noqa: E402
from analyst.core.pipeline import AnalystPipeline  # noqa: E402
from analyst.core.semantic import load_roles  # noqa: E402

st.set_page_config(page_title="Payments AI Analyst", page_icon="📊", layout="wide")

EXAMPLES = [
    "Why did success rate drop last week?",
    "Why did the success rate fall on 21 Oct?",
    "Show the daily success rate trend",
    "UPI success rate by channel last week",
    "Top 5 merchants by GMV last week",
    "Why did GMV change in the week of 26 Oct?",
    "What is the success rate in the South region?",
    "How are we doing?",
]
STATUS_STYLE = {
    "answered": ("success", "✅ Answered"),
    "needs_clarification": ("info", "❓ Needs clarification"),
    "denied": ("error", "🔒 Access denied"),
    "blocked": ("error", "🛑 Blocked by safety checks"),
    "error": ("warning", "⚠️ Could not answer"),
}


@st.cache_resource(show_spinner="Loading data and models…")
def get_pipeline() -> AnalystPipeline:
    return AnalystPipeline()


# --------------------------------------------------------------------------- charts
def _fmt_axis(fig, y_format: str):
    if y_format == "percent":
        fig.update_yaxes(tickformat=".1%")
    elif y_format == "pp":
        fig.update_yaxes(ticksuffix=" pp")
    elif y_format == "currency":
        fig.update_yaxes(tickprefix="₹", tickformat=",.0f")
    return fig


def render_chart(c: ChartSpec) -> None:
    df = pd.DataFrame(c.data)
    if df.empty:
        return
    if c.kind == "waterfall":
        fig = go.Figure(go.Waterfall(
            x=df["step"], y=df["value"],
            measure=["absolute" if k == "total" else "relative" for k in df["kind"]],
            text=[f"{v:+.2f}" if k == "delta" else f"{v:.2f}"
                  for v, k in zip(df["value"], df["kind"], strict=True)],
            textposition="outside",
            decreasing={"marker": {"color": "#d1495b"}},
            increasing={"marker": {"color": "#2e9e6b"}},
            totals={"marker": {"color": "#3a6ea5"}},
        ))
        lo = df.loc[df["kind"] == "total", "value"].min()
        span = df.loc[df["kind"] == "delta", "value"].abs().sum()
        fig.update_yaxes(range=[lo - 1.5 * span - 0.2, df["value"].max() + 0.6 * span + 0.2])
        fig.update_layout(title=c.title, showlegend=False, height=380,
                          yaxis_title="success rate (%)" if c.y_format == "pp" else c.y)
    elif c.kind == "grouped_bar":
        fig = px.bar(df, x=c.x, y=c.y, color=c.color, barmode="group", title=c.title,
                     color_discrete_sequence=["#9db4d0", "#3a6ea5"])
        fig.update_layout(height=360)
        fig = _fmt_axis(fig, c.y_format)
        if c.y_format == "percent" and c.y == "value":
            vals = df[c.y].dropna()
            if len(vals):
                fig.update_yaxes(range=[max(0, vals.min() - 0.05), min(1, vals.max() + 0.02)])
    elif c.kind == "line":
        fig = px.line(df, x=c.x, y=c.y, color=c.color, markers=True, title=c.title)
        if "anomaly" in df and df["anomaly"].any():
            a = df[df["anomaly"]]
            fig.add_scatter(x=a[c.x], y=a[c.y], mode="markers", name="unusual",
                            marker={"size": 13, "color": "#d1495b", "symbol": "x"})
        fig.update_layout(height=360)
        fig = _fmt_axis(fig, c.y_format)
    else:
        fig = px.bar(df, x=c.x, y=c.y, title=c.title, color_discrete_sequence=["#3a6ea5"],
                     hover_data=[col for col in ("den",) if col in df])
        fig.update_layout(height=380)
        fig = _fmt_axis(fig, c.y_format)
    st.plotly_chart(fig, width="stretch")


# --------------------------------------------------------------------------- answer view
def render_answer(a: Answer) -> None:
    kind, label = STATUS_STYLE[a.status]
    getattr(st, kind)(f"**{label}**  \n{a.summary}")

    if a.status == "needs_clarification" and a.clarification_options:
        st.caption("Try one of these:")
        cols = st.columns(len(a.clarification_options))
        for col, opt in zip(cols, a.clarification_options, strict=False):
            if col.button(opt, key=f"clar-{opt}"):
                st.session_state["question"] = opt
                st.rerun()

    if a.quality_warnings:
        with st.expander(f"⚠️ Data-quality checks ({len(a.quality_warnings)})", expanded=True):
            for w in a.quality_warnings:
                icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}[w.severity]
                st.markdown(f"{icon} **{w.check}** - {w.message}"
                            + (f"  `{w.query_id}`" if w.query_id else ""))

    if a.charts:
        charts = a.charts
        for i in range(0, len(charts), 2):
            cols = st.columns(2)
            for col, chart in zip(cols, charts[i:i + 2], strict=False):
                with col:
                    render_chart(chart)

    if a.facts or a.hypotheses or a.recommendations:
        c1, c2 = st.columns([3, 2])
        with c1:
            if a.facts:
                st.subheader("Observed facts")
                st.caption("Each fact cites the query, dataset, time window and filters behind it.")
                for f in a.facts:
                    st.markdown(f"- {f.statement}  \n"
                                f"  <small>`{f.query_id}` · {f.dataset} · {f.time_window} · "
                                f"filters: {f.filters}</small>", unsafe_allow_html=True)
        with c2:
            if a.hypotheses:
                st.subheader("Hypotheses")
                for h in a.hypotheses:
                    badge = {"high": "🟢 high", "medium": "🟡 medium", "low": "⚪ low"}[h.confidence]
                    st.markdown(f"**{h.statement}**  \n_Evidence:_ {h.evidence}  \n_Confidence:_ {badge}")
            if a.recommendations:
                st.subheader("Recommended next steps")
                for r in a.recommendations:
                    st.markdown(f"- {r}")

    if a.caveats:
        with st.expander("Assumptions & caveats", expanded=True):
            for c in a.caveats:
                st.markdown(f"- {c}")

    for t in a.tables:
        with st.expander(f"Table: {t.title}"):
            st.dataframe(pd.DataFrame(t.rows, columns=t.columns), width="stretch",
                         hide_index=True)

    if a.queries:
        with st.expander(f"🔎 Query evidence ({len(a.queries)} queries)"):
            for q in a.queries:
                st.markdown(f"**`{q.query_id}`** - {q.purpose}  ·  {q.row_count} rows  ·  "
                            f"{q.elapsed_ms:.0f} ms{'  ·  cached' if q.cached else ''}")
                if q.controls:
                    st.caption("Safety/access controls applied: " + "; ".join(q.controls))
                tab1, tab2 = st.tabs(["Executed SQL", "Before access rewrite"])
                tab1.code(q.sql, language="sql")
                tab2.code(q.original_sql, language="sql")

    with st.expander("⏱️ Pipeline trace"):
        st.dataframe(pd.DataFrame([t.model_dump() for t in a.trace]), width="stretch",
                     hide_index=True)
        if a.plan:
            st.caption("Plan")
            st.json(a.plan.model_dump(mode="json"), expanded=False)
        if a.error:
            st.caption("Error")
            st.json(a.error)


# --------------------------------------------------------------------------- layout
def main() -> None:
    pipe = get_pipeline()
    _, users = load_roles()

    with st.sidebar:
        st.title("📊 Payments AI Analyst")
        username = st.selectbox("Signed in as", list(users),
                                format_func=lambda u: users[u].display, index=1)
        st.caption(users[username].role.description)
        st.divider()
        st.markdown(f"**Mode:** `{pipe.mode}`")
        st.markdown(f"**Data loaded through:** {pipe.catalog.as_of:%d %b %Y %H:%M}")
        st.divider()
        st.markdown("**Example questions**")
        for ex in EXAMPLES:
            if st.button(ex, key=f"ex-{ex}", width="stretch"):
                st.session_state["question"] = ex
                st.session_state["autorun"] = True

    ask_tab, sql_tab, about_tab = st.tabs(["Ask a question", "SQL console", "About the data"])

    with ask_tab:
        q = st.text_input("Ask about payment performance",
                          key="question", placeholder="e.g. Why did success rate drop last week?")
        go_clicked = st.button("Analyse", type="primary")
        if (go_clicked or st.session_state.pop("autorun", False)) and q.strip():
            with st.spinner("Planning, checking data quality, running queries…"):
                st.session_state["answer"] = pipe.ask(q, username)
        if "answer" in st.session_state:
            render_answer(st.session_state["answer"])

    with sql_tab:
        st.caption("Power-user console. Your SQL goes through the same safety checker and access "
                   "rules as AI-generated SQL.")
        sql = st.text_area("SQL", height=160,
                           value="SELECT payment_method, COUNT(*) AS attempts\n"
                                 "FROM (SELECT DISTINCT * FROM transactions)\nGROUP BY 1\nORDER BY 2 DESC")
        if st.button("Run SQL"):
            render_answer(pipe.run_sql(sql, username))

    with about_tab:
        st.markdown("Synthetic Pine Labs-style payments data: 8 weeks, ~350K transactions, "
                    "400 merchants, 8 fictional banks, POS and online channels. "
                    "Problems are planted on purpose - see the README for the answer key.")
        st.json(pipe.semantic.describe().split("\n"), expanded=False)


main()
