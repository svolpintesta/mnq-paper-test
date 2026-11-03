END-OF-DAY PAPER TEST (no Quantower, no broker, no live feed)

WHAT IT IS
 Each evening it pulls today's finished 1-minute MNQ bars, runs the exact same strategy engine bar by bar (Gap and Go, ORB, NR7; one position at a time),
 and adds the day to ledger.csv with a short report. Missed days are caught up automatically. This is a genuine out-of-sample test:
 the engine only ever sees bars up to the moment of each decision. It does NOT test real fills; it assumes a market entry at the next open plus 0.5 point.

DATA SOURCE (pick one)
 1. Databento (recommended). You already have an account (the SPY/QQQ/DIA files). New accounts get $125 of free credit; the script asks Databento for the
    price first and refuses to download anything over $1 (--max-cost). A day of one-symbol 1-minute bars should cost a tiny fraction of a cent,
    but check the quote printed in the log. Credits are one-time, not recurring: look at your balance in the Databento portal.
    First run pulls ~45 days so the engine has 20 sessions of history; those warm-up days are paper-traded too.
 2. Yahoo (no account): use --source yahoo. UNOFFICIAL, only about 7 days of 1-minute history, so the first 20 trading days produce no trades
    while it builds history, and it cannot see contract changes (it skips Thursday, Friday and Monday around each quarterly expiry instead).

RUN IT FREE IN THE CLOUD (no PC needed)
 1. Make a free GitHub account, create a PRIVATE repository, upload everything in this folder (keep the .github folder).
 2. Repository Settings > Secrets and variables > Actions > New secret: DATABENTO_API_KEY = your key (starts with db-).
 3. Actions tab > "MNQ daily paper test" > Run workflow once to check it. After that it runs every weekday evening and saves results in the mnq_eod_live folder.
 GitHub pauses scheduled runs in a repository with no activity for 60 days; if that happens, press Enable in the Actions tab.
RUN IT ON YOUR PC
 pip install databento pandas tzdata ; setx DATABENTO_API_KEY "db-..." ; double-click INSTALL_EOD_TASK.bat

OPTIONAL: Telegram message each evening with the result (alerts only). Create a bot with BotFather and set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (as GitHub secrets or PC environment variables).

READING THE RESULTS
 mnq_eod_live/ledger.csv (every paper trade) and report_DATE.md. Backtest expectation for this set: about $28 per trade, 44% winners, profit factor 1.38.
 Judge nothing before about 60 trades (roughly 3 months). A few losing weeks are normal for these strategies.

NOT VERIFIED HERE: the Databento and Yahoo downloads (my environment cannot reach them), the GitHub workflow, the .bat files. The engine, catch-up, rerun, holiday skip and ledger were tested on real NQ data.
