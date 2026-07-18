from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


DATA_PATH = Path("academia_treinos_whatsapp.csv")
COLOR_SEQUENCE = [
    "#FF3D3D",
    "#FF3D81",
    "#FFB000",
    "#39FF88",
    "#B26DFF",
    "#FF5A36",
    "#00D084",
    "#F4F72A",
    "#FF7A00",
]
PLOTLY_TEMPLATE = "plotly_dark"
GROUP_CHART_FEATURES = {
    "peso_kg": {"label": "Peso (kg)", "color": "#FF3D3D", "decimals": 1},
    "repeticoes_melhor_serie": {"label": "Reps melhor serie", "color": "#FFB000", "decimals": 0},
    "series": {"label": "Series", "color": "#39FF88", "decimals": 0},
    "repeticoes_totais": {"label": "Reps totais", "color": "#FF3D81", "decimals": 0},
    "estimativa_1rm": {"label": "1RM estimado", "color": "#B26DFF", "decimals": 1},
    "volume": {"label": "Volume", "color": "#FF7A00", "decimals": 0},
}


st.set_page_config(
    page_title="Gym Tracker",
    page_icon="",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp {
        background:
            radial-gradient(circle at 10% 0%, rgba(255, 61, 61, 0.16), transparent 28%),
            radial-gradient(circle at 88% 10%, rgba(255, 61, 129, 0.16), transparent 28%),
            linear-gradient(135deg, #070A12 0%, #0D111F 48%, #111827 100%);
        color: #F8FAFC;
    }

    [data-testid="stSidebar"] {
        background: #070A12;
        border-right: 1px solid rgba(148, 163, 184, 0.2);
    }

    [data-testid="stMetric"] {
        background: rgba(15, 23, 42, 0.78);
        border: 1px solid rgba(255, 61, 61, 0.28);
        border-radius: 12px;
        padding: 14px 16px;
        box-shadow: 0 18px 40px rgba(0, 0, 0, 0.28);
    }

    [data-testid="stMetricValue"] {
        color: #FF4D4D;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }

    .stTabs [data-baseweb="tab"] {
        border-radius: 999px;
        background: rgba(15, 23, 42, 0.72);
        border: 1px solid rgba(148, 163, 184, 0.18);
        padding: 8px 16px;
    }

    div[data-testid="stDataFrame"] {
        border: 1px solid rgba(148, 163, 184, 0.22);
        border-radius: 12px;
        overflow: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data
def load_data(path: Path, modified_at: float) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["data"] = pd.to_datetime(df["data"])
    df["peso_kg"] = pd.to_numeric(df["peso_kg"], errors="coerce")
    df["serie"] = pd.to_numeric(df["serie"], errors="coerce").astype("Int64")
    df["repeticoes"] = pd.to_numeric(df["repeticoes"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["data", "peso_kg", "repeticoes"])

    df["volume"] = df["peso_kg"] * df["repeticoes"]
    df["estimativa_1rm"] = df["peso_kg"] * (1 + df["repeticoes"] / 30)
    df["semana"] = df["data"].dt.to_period("W-MON").dt.start_time
    df["mes"] = df["data"].dt.to_period("M").dt.to_timestamp()
    df["exercicio_tipo"] = df["exercicio"] + " - " + df["tipo"]
    return df


def format_number(value: float, decimals: int = 0) -> str:
    if pd.isna(value):
        return "-"
    return f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def metric_card(label: str, value: str, help_text: str | None = None) -> None:
    st.metric(label=label, value=value, help=help_text)


def filter_multiselect(label: str, options: list[str], default_all: bool = True) -> list[str]:
    default = options if default_all else []
    return st.sidebar.multiselect(label, options=options, default=default)


def select_all_or_one(label: str, options: list[str]) -> list[str]:
    choice = st.sidebar.selectbox(label, options=["Todos"] + options)
    return options if choice == "Todos" else [choice]


def theme_figure(fig):
    fig.update_layout(
        template=PLOTLY_TEMPLATE,
        colorway=COLOR_SEQUENCE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#E5E7EB"},
        legend_title_text="",
        hovermode="x unified",
        margin={"l": 20, "r": 20, "t": 40, "b": 20},
    )
    fig.update_xaxes(gridcolor="rgba(148, 163, 184, 0.16)", zerolinecolor="rgba(148, 163, 184, 0.2)")
    fig.update_yaxes(gridcolor="rgba(148, 163, 184, 0.16)", zerolinecolor="rgba(148, 163, 184, 0.2)")
    return fig


def daily_progress(data: pd.DataFrame) -> pd.DataFrame:
    return (
        data.groupby(["data", "exercicio", "tipo", "exercicio_tipo"], as_index=False)
        .agg(
            volume=("volume", "sum"),
            peso_kg=("peso_kg", "max"),
            repeticoes=("repeticoes", "sum"),
            series=("serie", "count"),
            estimativa_1rm=("estimativa_1rm", "max"),
        )
        .sort_values("data")
    )


def top_set_progress(data: pd.DataFrame) -> pd.DataFrame:
    return (
        data.sort_values(["data", "peso_kg", "repeticoes"], ascending=[True, False, False])
        .groupby("data", as_index=False)
        .first()
        .sort_values("data")
    )


def exercise_day_progress(data: pd.DataFrame) -> pd.DataFrame:
    top_sets = top_set_progress(data)[["data", "peso_kg", "repeticoes", "estimativa_1rm"]]
    top_sets = top_sets.rename(columns={"repeticoes": "repeticoes_melhor_serie"})
    daily_totals = data.groupby("data", as_index=False).agg(
        series=("serie", "count"),
        repeticoes_totais=("repeticoes", "sum"),
        volume=("volume", "sum"),
    )
    progress = top_sets.merge(daily_totals, on="data", how="left")
    progress["repeticoes_melhor_serie"] = progress["repeticoes_melhor_serie"].astype(int)
    progress["repeticoes_totais"] = progress["repeticoes_totais"].astype(int)
    progress["series"] = progress["series"].astype(int)
    return progress.sort_values("data")


def feature_display(value: float, decimals: int) -> str:
    if decimals == 0:
        return format_number(int(round(value)))
    return format_number(value, decimals)


def build_feature_chart_data(progress: pd.DataFrame, selected_features: list[str], normalize: bool) -> pd.DataFrame:
    rows = []
    for feature in selected_features:
        config = GROUP_CHART_FEATURES[feature]
        series = progress[feature].astype(float)
        max_value = series.max()
        min_value = series.min()

        if normalize:
            if max_value == min_value:
                plot_values = pd.Series([100.0] * len(series), index=series.index)
            else:
                plot_values = ((series - min_value) / (max_value - min_value)) * 100
        else:
            plot_values = series

        for index, raw_value in series.items():
            rows.append(
                {
                    "data": progress.loc[index, "data"],
                    "feature": feature,
                    "metrica": config["label"],
                    "valor": raw_value,
                    "valor_plot": plot_values.loc[index],
                    "valor_formatado": feature_display(raw_value, config["decimals"]),
                }
            )

    return pd.DataFrame(rows)


def metric_axis_label(metric: str) -> str:
    return {
        "peso_kg": "Maior carga",
        "repeticoes": "Repeticoes totais",
        "series": "Series",
        "estimativa_1rm": "1RM estimado",
    }[metric]


if not DATA_PATH.exists():
    st.error(f"Arquivo nao encontrado: {DATA_PATH}")
    st.stop()


df = load_data(DATA_PATH, DATA_PATH.stat().st_mtime)

st.title("Gym Tracker")
st.caption("Base importada do WhatsApp, com uma linha por serie.")

with st.sidebar:
    st.header("Filtros")

    min_date = df["data"].min().date()
    max_date = df["data"].max().date()
    date_range = st.date_input(
        "Periodo",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
    )

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = min_date, max_date

    all_groups = sorted(df["grupo_muscular"].unique())
    groups = select_all_or_one("Grupo muscular", all_groups)

    available_after_group = df[df["grupo_muscular"].isin(groups)] if groups else df.iloc[0:0]
    exercises = filter_multiselect("Exercicio", sorted(available_after_group["exercicio"].unique()))

    available_after_exercise = available_after_group[available_after_group["exercicio"].isin(exercises)] if exercises else available_after_group.iloc[0:0]
    types = filter_multiselect("Tipo", sorted(available_after_exercise["tipo"].unique()))

    metric_choice = st.selectbox(
        "Metrica principal",
        options=[
            "peso_kg",
            "repeticoes",
            "series",
            "estimativa_1rm",
        ],
        format_func={
            "peso_kg": "Carga",
            "repeticoes": "Repeticoes",
            "series": "Series",
            "estimativa_1rm": "1RM estimado",
        }.get,
    )

mask = (
    (df["data"].dt.date >= start_date)
    & (df["data"].dt.date <= end_date)
    & (df["grupo_muscular"].isin(groups))
    & (df["exercicio"].isin(exercises))
    & (df["tipo"].isin(types))
)
filtered = df.loc[mask].copy()

if filtered.empty:
    st.warning("Nenhum registro encontrado para os filtros atuais.")
    st.stop()

days_count = filtered["data"].nunique()
series_count = len(filtered)
volume_total = filtered["volume"].sum()
reps_total = int(filtered["repeticoes"].sum())
best_load = filtered["peso_kg"].max()
best_1rm = filtered["estimativa_1rm"].max()

kpi_cols = st.columns(5)
with kpi_cols[0]:
    metric_card("Dias registrados", str(days_count))
with kpi_cols[1]:
    metric_card("Series", str(series_count))
with kpi_cols[2]:
    metric_card("Repeticoes", format_number(reps_total))
with kpi_cols[3]:
    metric_card("Maior carga", f"{format_number(best_load, 1)} kg")
with kpi_cols[4]:
    metric_card("Maior 1RM estimado", f"{format_number(best_1rm, 1)} kg", "Formula de Epley: peso x (1 + reps/30)")

with st.expander("O que e 1RM estimado?"):
    st.write(
        "1RM estimado e uma estimativa de quanto peso voce conseguiria levantar em uma repeticao maxima. "
        "Aqui uso a formula de Epley: peso x (1 + repeticoes / 30). "
        "E util para comparar progresso quando voce muda reps e carga, mas nao e uma medida perfeita."
    )

with st.expander("Supino reto, supino maquina e press sao a mesma coisa?"):
    st.write(
        "Nao exatamente. Supino reto com halter mede o movimento livre, com mais estabilizacao. "
        "Supino reto na maquina segue o caminho do aparelho. Press peito/triceps ficou separado porque as anotacoes "
        "indicavam uma maquina parecida com press/chest press, mas com participacao forte de triceps. "
        "Para acompanhar progresso, compare cada variacao dentro do mesmo tipo."
    )

tab_overview, tab_groups, tab_progress, tab_records, tab_data = st.tabs(
    ["Visao geral", "Grupos", "Evolucao", "Records", "Dados"]
)

with tab_overview:
    left, right = st.columns((1.2, 1))

    weekly_group = (
        filtered.groupby(["semana", "grupo_muscular"], as_index=False)
        .agg(series=("serie", "count"), repeticoes=("repeticoes", "sum"), volume=("volume", "sum"))
        .sort_values("semana")
    )

    with left:
        st.subheader("Series semanais por grupo")
        fig = px.bar(
            weekly_group,
            x="semana",
            y="series",
            color="grupo_muscular",
            barmode="stack",
            color_discrete_sequence=COLOR_SEQUENCE,
            labels={
                "semana": "Semana",
                "series": "Series",
                "grupo_muscular": "Grupo",
            },
        )
        theme_figure(fig)
        st.plotly_chart(fig, use_container_width=True)

    group_summary = (
        filtered.groupby("grupo_muscular", as_index=False)
        .agg(
            volume=("volume", "sum"),
            series=("serie", "count"),
            repeticoes=("repeticoes", "sum"),
            treinos=("data", "nunique"),
            maior_carga=("peso_kg", "max"),
        )
        .sort_values("series", ascending=False)
    )

    with right:
        st.subheader("Series por grupo")
        fig = px.bar(
            group_summary,
            x="series",
            y="grupo_muscular",
            color="grupo_muscular",
            orientation="h",
            color_discrete_sequence=COLOR_SEQUENCE,
            labels={"series": "Series", "grupo_muscular": "Grupo"},
        )
        theme_figure(fig)
        fig.update_layout(showlegend=False, yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Resumo por grupo")
    st.dataframe(
        group_summary.rename(
            columns={
                "grupo_muscular": "Grupo",
                "series": "Series",
                "repeticoes": "Repeticoes",
                "treinos": "Dias",
                "maior_carga": "Maior carga",
                "volume": "Volume",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

with tab_groups:
    st.subheader("Paineis por grupo muscular")
    st.caption("Escolha exercicios dentro de cada grupo para ver a evolucao sem misturar movimentos de grupos diferentes.")

    group_names = sorted(filtered["grupo_muscular"].unique())
    group_tabs = st.tabs(group_names)

    for group_name, group_tab in zip(group_names, group_tabs):
        with group_tab:
            group_data = filtered[filtered["grupo_muscular"] == group_name].copy()
            group_exercises = sorted(group_data["exercicio"].unique())
            selected_group_exercises = st.multiselect(
                "Exercicios do grupo",
                options=group_exercises,
                default=group_exercises,
                key=f"group_exercises_{group_name}",
            )
            group_view = group_data[group_data["exercicio"].isin(selected_group_exercises)].copy()

            if group_view.empty:
                st.info("Selecione pelo menos um exercicio para ver este painel.")
                continue

            gcols = st.columns(4)
            with gcols[0]:
                metric_card("Volume do grupo", f"{format_number(group_view['volume'].sum())} kgxrep")
            with gcols[1]:
                metric_card("Exercicios", str(group_view["exercicio"].nunique()))
            with gcols[2]:
                metric_card("Series", str(len(group_view)))
            with gcols[3]:
                metric_card("Melhor carga", f"{format_number(group_view['peso_kg'].max(), 1)} kg")

            exercise_options = sorted(group_view["exercicio_tipo"].unique())
            focused_exercise = st.selectbox(
                "Exercicio para evolucao",
                options=exercise_options,
                key=f"focused_exercise_{group_name}",
            )
            focus_view = group_view[group_view["exercicio_tipo"] == focused_exercise]
            progress = exercise_day_progress(focus_view)

            chart_left, chart_right = st.columns((1.4, 1))
            with chart_left:
                selected_features = st.multiselect(
                    "Metricas no grafico",
                    options=list(GROUP_CHART_FEATURES.keys()),
                    default=["peso_kg", "repeticoes_melhor_serie", "series"],
                    format_func=lambda key: GROUP_CHART_FEATURES[key]["label"],
                    key=f"group_chart_features_{group_name}",
                )
            with chart_right:
                scale_mode = st.radio(
                    "Escala",
                    options=["Valores reais", "Normalizado 0-100"],
                    horizontal=True,
                    key=f"group_chart_scale_{group_name}",
                )

            if not selected_features:
                st.info("Escolha pelo menos uma metrica para desenhar o grafico.")
            else:
                normalize_chart = scale_mode == "Normalizado 0-100"
                chart_data = build_feature_chart_data(progress, selected_features, normalize_chart)
                color_map = {
                    config["label"]: config["color"]
                    for config in GROUP_CHART_FEATURES.values()
                }
                fig = px.line(
                    chart_data,
                    x="data",
                    y="valor_plot",
                    color="metrica",
                    markers=True,
                    color_discrete_map=color_map,
                    custom_data=["metrica", "valor_formatado"],
                    labels={
                        "data": "Data",
                        "valor_plot": "Escala normalizada (0-100)" if normalize_chart else "Valor",
                        "metrica": "Metrica",
                    },
                )
                fig.update_traces(
                    line={"width": 3},
                    marker={"size": 9},
                    hovertemplate="<b>%{customdata[0]}</b><br>Data: %{x|%d/%m/%Y}<br>Valor: %{customdata[1]}<extra></extra>",
                )
                theme_figure(fig)
                fig.update_layout(
                    title=f"Evolucao: {focused_exercise}",
                    hovermode="x unified",
                )
                if not normalize_chart and all(GROUP_CHART_FEATURES[feature]["decimals"] == 0 for feature in selected_features):
                    fig.update_yaxes(tickformat="d")
                st.plotly_chart(fig, use_container_width=True)

            with st.expander("Como ler este grafico"):
                st.write(
                    "Em valores reais, cada linha usa a propria unidade da metrica escolhida. "
                    "Se as escalas ficarem muito diferentes, use `Normalizado 0-100`: cada linha passa a mostrar a evolucao relativa dentro do periodo selecionado."
                )

            exercise_summary = (
                group_view.groupby(["exercicio", "tipo"], as_index=False)
                .agg(
                    volume=("volume", "sum"),
                    series=("serie", "count"),
                    repeticoes=("repeticoes", "sum"),
                    dias=("data", "nunique"),
                    maior_carga=("peso_kg", "max"),
                    melhor_1rm=("estimativa_1rm", "max"),
                )
                .sort_values(["exercicio", "tipo"])
            )
            st.dataframe(
                exercise_summary.rename(
                    columns={
                        "exercicio": "Exercicio",
                        "tipo": "Tipo",
                        "series": "Series",
                        "repeticoes": "Repeticoes",
                        "dias": "Dias",
                        "maior_carga": "Maior carga",
                        "melhor_1rm": "Melhor 1RM",
                        "volume": "Volume",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

with tab_progress:
    st.subheader("Evolucao por exercicio e tipo")

    daily = daily_progress(filtered)

    fig = px.line(
        daily,
        x="data",
        y=metric_choice,
        color="exercicio_tipo",
        markers=True,
        color_discrete_sequence=COLOR_SEQUENCE,
        labels={
            "data": "Data",
            metric_choice: metric_axis_label(metric_choice),
            "exercicio_tipo": "Exercicio",
        },
    )
    theme_figure(fig)
    if metric_choice in {"repeticoes", "series"}:
        fig.update_yaxes(tickformat="d")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Series mensais")
    monthly = (
        filtered.groupby(["mes", "grupo_muscular"], as_index=False)
        .agg(series=("serie", "count"), repeticoes=("repeticoes", "sum"))
        .sort_values("mes")
    )
    fig = px.bar(
        monthly,
        x="mes",
        y="series",
        color="grupo_muscular",
        color_discrete_sequence=COLOR_SEQUENCE,
        labels={"mes": "Mes", "series": "Series", "grupo_muscular": "Grupo"},
    )
    theme_figure(fig)
    fig.update_yaxes(tickformat="d")
    st.plotly_chart(fig, use_container_width=True)

with tab_records:
    st.subheader("Melhores marcas por exercicio")

    records = (
        filtered.sort_values(["peso_kg", "repeticoes", "estimativa_1rm"], ascending=False)
        .groupby(["exercicio", "tipo"], as_index=False)
        .first()
        .sort_values(["grupo_muscular", "exercicio", "tipo"])
    )

    records_table = records[
        [
            "grupo_muscular",
            "exercicio",
            "tipo",
            "data",
            "peso_kg",
            "repeticoes",
            "estimativa_1rm",
            "volume",
        ]
    ].rename(
        columns={
            "grupo_muscular": "Grupo",
            "exercicio": "Exercicio",
            "tipo": "Tipo",
            "data": "Data",
            "peso_kg": "Maior carga",
            "repeticoes": "Reps",
            "estimativa_1rm": "1RM estimado",
            "volume": "Volume da serie",
        }
    )
    st.dataframe(records_table, use_container_width=True, hide_index=True)

    st.subheader("Top series por 1RM estimado")
    top_sets = filtered.nlargest(20, "estimativa_1rm")[
        [
            "data",
            "grupo_muscular",
            "exercicio",
            "tipo",
            "peso_kg",
            "repeticoes",
            "estimativa_1rm",
        ]
    ].rename(
        columns={
            "data": "Data",
            "grupo_muscular": "Grupo",
            "exercicio": "Exercicio",
            "tipo": "Tipo",
            "peso_kg": "Peso",
            "repeticoes": "Reps",
            "estimativa_1rm": "1RM estimado",
        }
    )
    st.dataframe(top_sets, use_container_width=True, hide_index=True)

with tab_data:
    st.subheader("Dados filtrados")
    st.dataframe(
        filtered.sort_values(["data", "grupo_muscular", "exercicio", "serie"]),
        use_container_width=True,
        hide_index=True,
    )

    csv_bytes = filtered.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Baixar CSV filtrado",
        data=csv_bytes,
        file_name="academia_filtrado.csv",
        mime="text/csv",
    )
