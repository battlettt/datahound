# DataHound

A local CLI that sniffs out data quality problems in a CSV or SQLite table and tracks them run over run, so bad data gets caught before it reaches a dashboard.

DataHound profiles every column (types, null rates, duplicates, ranges, top values), runs built-in and custom quality rules, saves each run to a local history store, and flags drift when a metric moves. Terminal summary plus an HTML report.

Local CLI, Python standard library only, works offline on any CSV or SQLite table.

Built with Claude Opus as portfolio project 27 (Data Engineering / Analytics).
