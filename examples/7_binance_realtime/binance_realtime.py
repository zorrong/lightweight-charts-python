import json
import threading
from time import sleep
from urllib.request import urlopen

import pandas as pd
from lightweight_charts import Chart

try:
    from websocket import WebSocketApp
except ImportError as e:
    raise SystemExit(
        "Missing dependency: websocket-client. Install it with 'pip install websocket-client' and rerun."  # noqa
    )


BINANCE_REST = "https://api.binance.com/api/v3/klines"
BINANCE_WS = "wss://stream.binance.com:9443/ws/{stream}"


def calculate_sma(df: pd.DataFrame, period: int = 50) -> pd.DataFrame:
    s = df['close'].rolling(window=period).mean()
    return pd.DataFrame({'time': df['time'], 'value': s})


def calculate_ema(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    s = df['close'].ewm(span=period, adjust=False).mean()
    return pd.DataFrame({'time': df['time'], 'value': s})


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    close = df['close']
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return pd.DataFrame({'time': df['time'], 'value': rsi})


def fetch_klines(symbol: str, interval: str = "1m", limit: int = 500) -> pd.DataFrame:
    url = f"{BINANCE_REST}?symbol={symbol}&interval={interval}&limit={limit}"
    with urlopen(url) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    cols = [
        "open_time","open","high","low","close","volume",
        "close_time","quote_asset_volume","number_of_trades",
        "taker_buy_base_asset_volume","taker_buy_quote_asset_volume","ignore"
    ]
    df = pd.DataFrame(data, columns=cols)

    # Convert/rename to match library expectations
    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ("open","high","low","close","volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df[["time","open","high","low","close","volume"]]


def start_ws(chart: Chart, symbol: str, interval: str = "1m"):
    stream = f"{symbol.lower()}@kline_{interval}"
    url = BINANCE_WS.format(stream=stream)

    def on_message(ws, message):
        try:
            data = json.loads(message)
            k = data.get("k") or data.get("data", {}).get("k")  # support single or combined stream
            if not k:
                return
            ts = pd.to_datetime(k["t"], unit="ms")  # ms timestamp -> pandas Timestamp
            series = pd.Series({
                "time": ts,
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"]),
            })
            # Update main series on chart
            chart.update(series)

            # Maintain local DF for indicators
            try:
                df = getattr(chart, "_df", None)
                if df is None or df.empty:
                    chart._df = pd.DataFrame([series])[['time','open','high','low','close','volume']]
                    df = chart._df
                else:
                    if df.iloc[-1]['time'] == ts:
                        # update last bar in-place
                        df.loc[df.index[-1], ['open','high','low','close','volume']] = [
                            series['open'], series['high'], series['low'], series['close'], series['volume']
                        ]
                    else:
                        chart._df = pd.concat([
                            df,
                            pd.DataFrame([series])[['time','open','high','low','close','volume']]
                        ], ignore_index=True)
                        df = chart._df
            except Exception:
                pass

            # Live-update indicators if active
            inds = getattr(chart, '_indicators', {})
            try:
                if 'SMA 50' in inds:
                    sma = calculate_sma(df, 50).dropna()
                    if not sma.empty:
                        inds['SMA 50'].update(sma.iloc[-1])
                if 'EMA 20' in inds:
                    ema = calculate_ema(df, 20).dropna()
                    if not ema.empty:
                        inds['EMA 20'].update(ema.iloc[-1])
                if 'RSI 14' in inds:
                    rsi = calculate_rsi(df, 14).dropna()
                    if not rsi.empty:
                        inds['RSI 14'].update(rsi.iloc[-1])
            except Exception:
                pass
        except Exception as e:
            print(f"[binance ws] on_message error: {e}")

    def on_error(ws, error):
        print(f"[binance ws] error: {error}")

    def on_close(ws, status_code, msg):
        print(f"[binance ws] closed: {status_code} {msg}")

    ws = WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
    # expose ws so we can close on timeframe switch
    chart._ws = ws
    ws.run_forever()


if __name__ == "__main__":
    symbol = "BTCUSDT"   # e.g. BTCUSDT, ETHUSDT
    interval = "15m"      # e.g. 1s/1m/5m/15m/1h/4h/1d depending on Binance support
    
    # Seed with recent historical bars
    df = fetch_klines(symbol, interval, limit=500)

    chart = Chart()
    chart.legend(True)
    chart.watermark(f"{symbol} {interval} • Binance", font_size=32, color='rgba(180, 180, 200, 0.35)')
    # đặt watermark ở giữa và thêm nút bật/tắt trong TopBar
    chart.run_script(f"""
      {chart.id}.chart.applyOptions({{
          watermark: {{
              visible: true,
              horzAlign: 'center',
              vertAlign: 'center',
              color: 'rgba(180, 180, 200, 0.35)',
              fontSize: 32,
              text: '{symbol} {interval} • Binance'
          }}
      }})
    """)

    # state for TF/WS/Indicators
    chart._symbol = symbol
    chart._interval = interval
    chart._df = df.copy()
    chart._ws = None
    chart._ws_thread = None
    chart._indicators = {}
    chart._rsi_chart = None

    # Toggle watermark button
    def _on_wm_toggle(c: Chart):
        pressed = c.topbar['wm'].value
        visible = not pressed  # pressed True => hide; pressed False => show
        c.run_script(f"{c.id}.chart.applyOptions({{watermark: {{ visible: {str(visible).lower()} }} }})")
        c.topbar['wm'].set(f"Watermark: {'On' if visible else 'Off'}")

    chart.topbar.button('wm', 'Watermark: On', align='right', toggle=True, func=_on_wm_toggle)

    # Change symbol textbox
    def _on_symbol_change(c: Chart):
        new_sym = str(c.topbar['symbol'].value).strip().upper()
        if not new_sym or new_sym == getattr(c, '_symbol', None):
            return
        try:
            new_df = fetch_klines(new_sym, c._interval, limit=500)
        except Exception as e:
            print(f"[symbol] fetch error for {new_sym}: {e}")
            # reset textbox to current symbol on failure
            c.topbar['symbol'].set(getattr(c, '_symbol', ''))
            return
        c._symbol = new_sym
        c._df = new_df.copy()
        c.set(new_df)
        # update watermark text
        c.run_script(f"{c.id}.chart.applyOptions({{watermark: {{ text: '{new_sym} {c._interval} • Binance' }} }})")
        # re-calc indicators if active
        if 'SMA 50' in c._indicators:
            c._indicators['SMA 50'].set(calculate_sma(c._df, 50).dropna())
        if 'EMA 20' in c._indicators:
            c._indicators['EMA 20'].set(calculate_ema(c._df, 20).dropna())
        if 'RSI 14' in c._indicators:
            c._indicators['RSI 14'].set(calculate_rsi(c._df, 14).dropna())
        # restart websocket
        try:
            if c._ws:
                c._ws.close()
        except Exception:
            pass
        t2 = threading.Thread(target=start_ws, args=(c, new_sym, c._interval), daemon=True)
        c._ws_thread = t2
        t2.start()

    chart.topbar.textbox('symbol', initial_text=symbol, align='left', func=_on_symbol_change)

    # Timeframe switcher
    def _on_tf_change(c: Chart):
        new_tf = c.topbar['tf'].value
        if new_tf == getattr(c, '_interval', None):
            return
        c._interval = new_tf
        new_df = fetch_klines(c._symbol, new_tf, limit=500)
        c._df = new_df.copy()
        c.set(new_df)
        # update watermark text
        c.run_script(f"{c.id}.chart.applyOptions({{watermark: {{ text: '{c._symbol} {new_tf} • Binance' }} }})")
        # re-calc indicators if active
        if 'SMA 50' in c._indicators:
            data = calculate_sma(c._df, 50).dropna()
            c._indicators['SMA 50'].set(data)
        if 'EMA 20' in c._indicators:
            data = calculate_ema(c._df, 20).dropna()
            c._indicators['EMA 20'].set(data)
        if 'RSI 14' in c._indicators:
            data = calculate_rsi(c._df, 14).dropna()
            c._indicators['RSI 14'].set(data)
        # restart websocket
        try:
            if c._ws:
                c._ws.close()
        except Exception:
            pass
        t2 = threading.Thread(target=start_ws, args=(c, c._symbol, new_tf), daemon=True)
        c._ws_thread = t2
        t2.start()

    chart.topbar.switcher('tf', ('1m','5m','15m','1h','4h','1d'), default=interval, align='left', func=_on_tf_change)

    # Indicator toggles
    def _toggle_sma50(c: Chart):
        pressed = c.topbar['sma50'].value
        name = 'SMA 50'
        if pressed and name not in c._indicators:
            line = c.create_line(name)
            data = calculate_sma(c._df, 50).dropna()
            line.set(data)
            c._indicators[name] = line
        elif not pressed and name in c._indicators:
            c._indicators[name].delete()
            del c._indicators[name]

    def _toggle_ema20(c: Chart):
        pressed = c.topbar['ema20'].value
        name = 'EMA 20'
        if pressed and name not in c._indicators:
            line = c.create_line(name)
            data = calculate_ema(c._df, 20).dropna()
            line.set(data)
            c._indicators[name] = line
        elif not pressed and name in c._indicators:
            c._indicators[name].delete()
            del c._indicators[name]

    def _toggle_rsi14(c: Chart):
        pressed = c.topbar['rsi14'].value
        name = 'RSI 14'
        if pressed and name not in c._indicators:
            # create subchart if not exists
            if getattr(c, '_rsi_chart', None) is None:
                c._rsi_chart = c.create_subchart(width=1, height=0.25, sync=True)
            line = c._rsi_chart.create_line(name, color='rgba(255, 214, 102, 0.9)', width=2)
            data = calculate_rsi(c._df, 14).dropna()
            line.set(data)
            # horizontal lines 70/30
            h70 = c._rsi_chart.horizontal_line(70, color='rgba(255,255,255,0.25)', width=1, style='dashed', text='70', axis_label_visible=True)
            h30 = c._rsi_chart.horizontal_line(30, color='rgba(255,255,255,0.25)', width=1, style='dashed', text='30', axis_label_visible=True)
            c._indicators[name] = line
            c._indicators[name + '_hlines'] = [h70, h30]
        elif not pressed and name in c._indicators:
            c._indicators[name].delete()
            del c._indicators[name]
            if name + '_hlines' in c._indicators:
                for hl in c._indicators[name + '_hlines']:
                    try:
                        hl.delete()
                    except Exception:
                        pass
                del c._indicators[name + '_hlines']

    chart.topbar.button('sma50', 'SMA 50', align='right', toggle=True, func=_toggle_sma50)
    chart.topbar.button('ema20', 'EMA 20', align='right', toggle=True, func=_toggle_ema20)
    chart.topbar.button('rsi14', 'RSI 14', align='right', toggle=True, func=_toggle_rsi14)

    chart.set(df)

    # Start websocket in background thread
    t = threading.Thread(target=start_ws, args=(chart, symbol, interval), daemon=True)
    chart._ws_thread = t
    t.start()

    # Show UI and block main thread (ws keeps running in daemon thread)
    chart.show(block=True)