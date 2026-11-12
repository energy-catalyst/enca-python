"""Last week's ground loop temperatures and flow, hourly, as a pandas DataFrame.

pip install -e '.[pandas]'
python examples/ground_loop_week.py
"""

from enca import Client

with Client() as c:
    hp = c.site().heat_pump
    frame = hp.history(
        ["ground_loop_in", "ground_loop_out", "ground_loop_flow", "cop"], last="7d", every="1h"
    )
    df = frame.to_dataframe()
    df["ground_loop_delta"] = df["ground_loop_in"] - df["ground_loop_out"]
    print(df.describe().round(2))
    df.to_csv("ground_loop_week.csv")
    print("Saved ground_loop_week.csv")
