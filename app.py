import os
import json
import sqlite3
import logging
from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import glob
from typing import List, Dict, Any

# Setup logging
logging.basicConfig(filename='app.log', level=logging.DEBUG,
                    format='%(asctime)s %(levelname)s: %(message)s')
logging.info("Starting application")

app = Flask(__name__)
DB_PATH = "database.db"

def init_db():
    logging.info("Initializing database")
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT, count INTEGER, buy_price INTEGER, total_cost INTEGER,
            bought_at TEXT, bought_system TEXT, bought_time TEXT,
            delivered INTEGER DEFAULT 0, delivered_to TEXT, delivered_time TEXT,
            sold INTEGER DEFAULT 0, sold_at TEXT, sold_time TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS processed_logs (
            filename TEXT PRIMARY KEY, processed_time TEXT
        )''')
        conn.commit()
        logging.info("Database initialized successfully")
    except sqlite3.Error as e:
        logging.error(f"Database initialization failed: {e}")
        raise
    finally:
        conn.close()

def load_config() -> Dict[str, Any]:
    logging.info("Loading config")
    filepath = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(filepath, "r") as f:
            config = json.load(f)
        logging.info(f"Config loaded successfully from {filepath}")
        return config
    except FileNotFoundError:
        logging.error(f"config.json not found at {filepath}")
        raise
    except json.JSONDecodeError as e:
        logging.error(f"Invalid config.json at {filepath}: {e}")
        raise

def parse_log_file(filepath: str) -> List[Dict[str, Any]]:
    events = []
    logging.info(f"Parsing log file: {filepath}")
    try:
        with open(filepath, "r") as f:
            for line in f:
                try:
                    event = json.loads(line.strip())
                    events.append(event)
                except json.JSONDecodeError as e:
                    logging.warning(f"Skipping invalid JSON line in {filepath}: {e}")
        logging.info(f"Parsed {len(events)} events from {filepath}")
    except IOError as e:
        logging.error(f"Failed to read log file {filepath}: {e}")
    return events

def parse_cargo_file(filepath: str) -> Dict[str, Any]:
    logging.info(f"Parsing cargo file: {filepath}")
    try:
        with open(filepath, "r") as f:
            cargo_data = json.load(f)
        logging.info(f"Parsed cargo data: {json.dumps(cargo_data)}")
        return cargo_data
    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Failed to parse cargo file {filepath}: {e}")
        return {"timestamp": "unknown", "Inventory": []}

def get_total_cargo_count(cargo_data: Dict[str, Any]) -> int:
    inventory = cargo_data.get("Inventory", [])
    total = sum(item.get("Count", 0) for item in inventory)
    logging.debug(f"Total cargo count: {total}")
    return total

def process_logs(log_files: List[str], config: Dict[str, Any]) -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    colonized_systems = {sys["SystemAddress"] for sys in config["colonized_systems"]}
    docked_station = None
    cargo_count = None
    log_dir = config["log_directory"]
    cargo_file = os.path.join(log_dir, "Cargo.json")

    logging.info(f"Processing {len(log_files)} log files")
    for log_file in log_files:
        filename = os.path.basename(log_file)
        c.execute("SELECT 1 FROM processed_logs WHERE filename = ?", (filename,))
        if c.fetchone():
            logging.debug(f"Skipping already processed log: {filename}")
            continue

        events = parse_log_file(log_file)
        for event in events:
            logging.debug(f"Processing event: {event['event']} at {event['timestamp']}")
            if event["event"] == "Docked":
                market_id = event.get("MarketID")
                if market_id is None:
                    logging.warning(f"Docked event missing MarketID: {json.dumps(event)}")
                    continue
                docked_station = {
                    "StationName": event["StationName"],
                    "StarSystem": event["StarSystem"],
                    "SystemAddress": event["SystemAddress"],
                    "timestamp": event["timestamp"]
                }
                logging.debug(f"Docked at {docked_station['StationName']} in {docked_station['StarSystem']}")
            elif event["event"] == "MarketBuy":
                market_id = event.get("MarketID")
                if not docked_station or market_id != event.get("MarketID"):
                    logging.warning(f"MarketBuy event with no prior docking or mismatched MarketID {market_id}: {json.dumps(event)}")
                    continue
                item_name = event.get("Type_Localised") or event.get("Type") or "Unknown"
                count = event.get("Count", 0)
                buy_price = event.get("BuyPrice", 0)
                total_cost = event.get("TotalCost", 0)
                try:
                    c.execute('''INSERT INTO purchases (item, count, buy_price, total_cost, bought_at, bought_system, bought_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''', (
                        item_name, count, buy_price, total_cost,
                        docked_station["StationName"], docked_station["StarSystem"], event["timestamp"]
                    ))
                    logging.debug(f"Added purchase: {item_name}, count: {count}")
                except sqlite3.Error as e:
                    logging.error(f"Database insert failed: {e}, event: {json.dumps(event)}")
                    continue
            elif event["event"] == "MarketSell":
                market_id = event.get("MarketID")
                if not docked_station or market_id != event.get("MarketID"):
                    logging.warning(f"MarketSell event with no prior docking or mismatched MarketID {market_id}: {json.dumps(event)}")
                    continue
                item_name = event.get("Type", "Unknown")
                count = event.get("Count", 0)
                logging.info(f"Detected sale of {count} {item_name} at {docked_station['StationName']}")
                c.execute('''UPDATE purchases SET sold = 1, sold_at = ?, sold_time = ?
                    WHERE item = ? AND count = ? AND delivered = 0 AND sold = 0 LIMIT 1''', (
                    docked_station["StationName"], event["timestamp"], item_name, count
                ))
                if c.rowcount > 0:
                    logging.info(f"Marked {c.rowcount} purchase(s) of {item_name} as sold")
                else:
                    logging.warning(f"No matching purchase found to mark as sold: {item_name}, count: {count}")
            elif event["event"] == "CargoDepot" and event.get("UpdateType") == "Deliver":
                market_id = event.get("EndMarketID")
                if not docked_station or market_id != event.get("EndMarketID"):
                    logging.warning(f"CargoDepot event with no prior docking or mismatched EndMarketID {market_id}: {json.dumps(event)}")
                    continue
                item_name = event.get("CargoType_Localised") or event.get("CargoType") or "Unknown"
                count = event.get("Count", 0)
                logging.info(f"Detected mission delivery of {count} {item_name} at {docked_station['StationName']}")
                c.execute('''UPDATE purchases SET delivered = 1, delivered_to = ?, delivered_time = ?
                    WHERE item = ? AND count = ? AND delivered = 0 AND sold = 0 LIMIT 1''', (
                    docked_station["StationName"], event["timestamp"], item_name, count
                ))
                if c.rowcount > 0:
                    logging.info(f"Marked {c.rowcount} purchase(s) of {item_name} as delivered via mission")
                else:
                    logging.warning(f"No matching purchase found to mark as delivered: {item_name}, count: {count}")
            elif event["event"] == "Cargo":
                cargo_data = parse_cargo_file(cargo_file)
                current_cargo = get_total_cargo_count(cargo_data)
                logging.debug(f"Cargo update triggered, total count: {current_cargo} from previous {cargo_count}")
                if current_cargo == 0 and cargo_count is not None and cargo_count > 0 and docked_station:
                    if docked_station["SystemAddress"] in colonized_systems:
                        logging.info(f"Detected non-mission delivery at {docked_station['StationName']} in {docked_station['StarSystem']} at {event['timestamp']}")
                        c.execute('''UPDATE purchases SET delivered = 1, delivered_to = ?, delivered_time = ?
                            WHERE delivered = 0 AND sold = 0''', (
                            docked_station["StationName"], event["timestamp"]
                        ))
                        logging.info(f"Updated {c.rowcount} purchases as delivered")
                cargo_count = current_cargo

        c.execute("INSERT INTO processed_logs (filename, processed_time) VALUES (?, ?)",
                  (filename, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        logging.info(f"Processed new log: {filename}")

    conn.close()

def scan_directory() -> None:
    try:
        config = load_config()
        log_dir = config["log_directory"]
        logging.info(f"Scanning directory: {log_dir}")
        if not os.path.isdir(log_dir):
            logging.error(f"Log directory does not exist: {log_dir}")
            raise FileNotFoundError(f"Log directory does not exist: {log_dir}")
        log_files = glob.glob(os.path.join(log_dir, "Journal.*.log"))
        log_files.sort()
        logging.info(f"Found {len(log_files)} log files")
        process_logs(log_files, config)
    except Exception as e:
        logging.error(f"Scan directory failed: {str(e)}")
        raise

@app.route("/", methods=["GET", "POST"])
def index():
    logging.info("Handling request to /")
    try:
        init_db()
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")
        return f"Database initialization failed: {str(e)}. Check app.log.", 500

    if request.method == "POST":
        if "log_files" in request.files:
            uploaded_files = request.files.getlist("log_files")
            log_paths = []
            for file in uploaded_files:
                if file.filename.endswith(".log"):
                    filepath = os.path.join("/tmp", file.filename)
                    file.save(filepath)
                    log_paths.append(filepath)
            if log_paths:
                try:
                    process_logs(log_paths, load_config())
                    for filepath in log_paths:
                        os.remove(filepath)
                    logging.info("Processed uploaded logs successfully")
                except Exception as e:
                    logging.error(f"Failed to process uploaded logs: {e}")
                    return f"Error processing logs: {str(e)}. Check app.log.", 500
            return redirect(url_for("index"))

    try:
        scan_directory()
    except Exception as e:
        logging.error(f"Failed to scan directory: {e}")
        return f"Error scanning logs: {str(e)}. Check app.log.", 500

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM purchases ORDER BY bought_time")
    purchases = [{"id": row[0], "item": row[1], "count": row[2], "buy_price": row[3],
                  "total_cost": row[4], "bought_at": row[5], "bought_system": row[6],
                  "bought_time": row[7], "delivered": bool(row[8]), "delivered_to": row[9],
                  "delivered_time": row[10], "sold": bool(row[11]), "sold_at": row[12],
                  "sold_time": row[13]} for row in c.fetchall()]
    c.execute("SELECT MAX(processed_time) FROM processed_logs")
    last_scan = c.fetchone()[0]
    conn.close()
    logging.info("Rendering index page")

    return render_template("index.html", purchases=purchases, last_scan=last_scan)

if __name__ == "__main__":
    logging.info("Starting Flask server")
    try:
        app.run(host="0.0.0.0", port=5000, debug=True)
    except Exception as e:
        logging.error(f"Failed to start Flask app: {e}")
        raise
