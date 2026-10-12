from pathlib import Path

import pandas as pd


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    data_dir = base_dir / "spider" / "data"
    out_dir = base_dir / "parquet_data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "guba_posts.parquet"

    if not data_dir.exists():
        print(f"数据目录不存在: {data_dir}")
        return

    all_frames: list[pd.DataFrame] = []
    csv_files = sorted(data_dir.glob("*_posts.csv"))
    if not csv_files:
        print(f"目录中未找到 *_posts.csv: {data_dir}")
        return

    print(f"发现 {len(csv_files)} 个 CSV 文件，开始合并为 Parquet...")

    for csv_path in csv_files:
        symbol = csv_path.stem.replace("_posts", "")
        try:
            df = pd.read_csv(csv_path, encoding="utf-8-sig")
        except Exception as e:  # noqa: BLE001
            print(f"读取失败，跳过 {csv_path}: {e}")
            continue

        if df is None or df.empty:
            continue

        df = df.copy()
        df["symbol"] = symbol
        all_frames.append(df)

    if not all_frames:
        print("没有可用数据，未生成 Parquet 文件。")
        return

    full = pd.concat(all_frames, ignore_index=True)
    full.to_parquet(out_path, index=False)
    print(f"已写入 {len(full)} 条记录到 {out_path}")


if __name__ == "__main__":
    main()
