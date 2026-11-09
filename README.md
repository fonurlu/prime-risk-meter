# PRIME Risk Meter

One number (0 = calm, 100 = crisis) built from 26 market, credit and economic indicators.

- **Web page:** https://fonurlu.github.io/prime-risk-meter/
- **Checks:** every 15 minutes (GitHub Actions, `.github/workflows/monitor.yml`)
- **Alerts:** a GitHub issue titled "PRIME Risk ALERT" is opened when an indicator scores 70+, the gauge changes zone, jumps 10 points in 24 h, or a key market line breaks (VIX > 30, S&P below 200-day, HY spread > 4.5%, VIX above 3-month VIX, Nasdaq -15%). GitHub emails the owner. An "all clear" comment closes it.
- **Edit levels/weights:** `config.json` · **manual values** (ISM, AAII, Bear-o-meter): `manual.json`

Not investment advice.
