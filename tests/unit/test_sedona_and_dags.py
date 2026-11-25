"""Unit tests validating Apache Sedona Spark SQL contract and Airflow DAG graph acyclicity."""

from __future__ import annotations

from processing.airflow.dags.geospatial_climate_dags import DAG_ID, TASK_DEPENDENCY_GRAPH
from processing.spark.sedona_geospatial_job import SedonaJobConfig, build_sedona_spatial_join_sql


def test_sedona_sql_contract_contains_geospatial_predicates() -> None:
    sql = build_sedona_spatial_join_sql(SedonaJobConfig())
    assert "ST_MakeValid" in sql
    assert "ST_GeomFromWKB" in sql
    assert "ST_Contains" in sql
    assert "ST_AreaSpheroid" in sql
    assert "event_type != 'RETRACTION'" in sql


def test_airflow_dag_dependency_graph_is_acyclic_and_complete() -> None:
    assert DAG_ID == "geospatial_climate_daily_lakehouse"
    # Kahn topological sort check for acyclicity
    indegree: dict[str, int] = {node: 0 for node in TASK_DEPENDENCY_GRAPH}
    for downstream_list in TASK_DEPENDENCY_GRAPH.values():
        for dst in downstream_list:
            indegree[dst] = indegree.get(dst, 0) + 1

    queue = [n for n, d in indegree.items() if d == 0]
    visited = 0
    while queue:
        cur = queue.pop(0)
        visited += 1
        for nxt in TASK_DEPENDENCY_GRAPH.get(cur, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    assert visited == len(TASK_DEPENDENCY_GRAPH)
