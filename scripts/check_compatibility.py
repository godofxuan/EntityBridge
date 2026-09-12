"""Exercise installed Splink, persistence, frozen TF, DuckDB and Parquet APIs."""
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

import duckdb
import pandas as pd
from splink import DuckDBAPI, Linker, SettingsCreator, block_on
from splink import comparison_library as cl


def smoke_records():
    rows = []
    names = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL"]
    for n in range(40):
        for source in ["gleif", "companies_house"]:
            name = f"{names[n % 8]} {n} INDUSTRIES LIMITED"
            if source == "companies_house" and n % 3 == 0:
                name = name.replace("INDUSTRIES", "INDUSTRY")
            rows.append({"record_id": f"{source}_{n}", "source": source, "name": name,
                         "postcode": f"AA{n}1AB", "address": f"{n} EXAMPLE ROAD"})
    return rows


def run():
    output = Path("artifacts/models/compatibility")
    output.mkdir(parents=True, exist_ok=True)
    settings = SettingsCreator(
        link_type="link_only", unique_id_column_name="record_id",
        source_dataset_column_name="source", retain_intermediate_calculation_columns=True,
        blocking_rules_to_generate_predictions=[block_on("postcode")],
        comparisons=[cl.JaroWinklerAtThresholds("name", [0.95, 0.85]).configure(term_frequency_adjustments=True),
                     cl.ExactMatch("postcode"), cl.JaroWinklerAtThresholds("address", [0.9, 0.7])],
        probability_two_random_records_match=0.025)
    rows = smoke_records()
    linker = Linker(pd.DataFrame(rows), settings, db_api=DuckDBAPI())
    linker.training.estimate_u_using_random_sampling(max_pairs=1600, seed=42)
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("postcode"))
    linker.training.estimate_parameters_using_expectation_maximisation(block_on("name"))
    linker.misc.save_model_to_json(output / "model.json", overwrite=True)
    tf = linker.table_management.compute_tf_table("name").as_pandas_dataframe()
    tf.to_parquet(output / "tf_name.parquet", index=False)
    before = linker.inference.predict().as_pandas_dataframe()
    frozen = json.loads((output / "model.json").read_text())
    extra = [{"record_id": "unrelated", "source": "gleif", "name": rows[0]["name"],
              "postcode": "ZZ99ZZ", "address": "UNRELATED"}]
    reloaded = Linker(pd.DataFrame(rows + extra), frozen, db_api=DuckDBAPI())
    reloaded.table_management.register_term_frequency_lookup(pd.read_parquet(output / "tf_name.parquet"), "name")
    after = reloaded.inference.predict().as_pandas_dataframe()
    columns = ["record_id_l", "record_id_r", "match_probability"]
    a = before[columns].sort_values(columns[:2]).reset_index(drop=True)
    b = after[columns].sort_values(columns[:2]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, atol=1e-12, rtol=1e-12)
    with duckdb.connect() as connection:
        count = connection.execute("SELECT count(*) FROM read_parquet(?)", [str(output / "tf_name.parquet")]).fetchone()[0]
    result = {"python": platform.python_version(), "versions": {
        package: importlib.metadata.version(package) for package in ["splink", "duckdb", "pandas", "pyarrow"]},
        "synthetic_only": True, "training_records": len(rows), "prediction_pairs": len(before),
        "frozen_tf_rows": count, "save_reload_unrelated_input_scores_equal": True,
        "model_sha256": hashlib.sha256((output / "model.json").read_bytes()).hexdigest()}
    report = Path("artifacts/reports/matching_compatibility.json")
    report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run()
