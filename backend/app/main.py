from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests
import math
import os
import json
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BASE_DIR, "data", "project.db")

app = FastAPI(
    title="Green Footprint & Carbon Offset Tracker",
    version="1.0.0"
)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

active_connections = set()


# ---------------- DATABASE ----------------

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pickup TEXT,
            destination TEXT,
            weight REAL,
            vehicle TEXT,
            vehicle_age REAL,
            distance_km REAL,
            duration_min REAL,
            carbon_kg REAL,
            created_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS gps_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            latitude REAL,
            longitude REAL,
            timestamp TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ---------------- MODELS ----------------

class LocationRequest(BaseModel):
    query: str


class RouteRequest(BaseModel):
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    weight: float = 5
    vehicle: str = "diesel_van"
    vehicle_age: float = 5


class CarbonRequest(BaseModel):
    distance_km: float
    weight: float
    vehicle: str
    vehicle_age: float
    slope_percent: float = 0
    traffic_factor: float = 1.0
    weather_factor: float = 1.0
    aqi: float = 50


# ---------------- HOME ----------------

@app.get("/")
def home():
    return FileResponse(os.path.join(BASE_DIR, "frontend", "index.html"))


# ---------------- GEOCODING ----------------

@app.get("/api/geocode")
def geocode(q: str):

    url = "https://nominatim.openstreetmap.org/search"

    params = {
        "q": q,
        "format": "json",
        "limit": 1
    }

    headers = {
        "User-Agent": "GreenFootprintTracker/1.0 college-project"
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        if not data:
            raise HTTPException(
                status_code=404,
                detail="Location not found"
            )

        return {
            "name": data[0]["display_name"],
            "lat": float(data[0]["lat"]),
            "lon": float(data[0]["lon"])
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=503,
            detail="Geocoding service unavailable"
        )


# ---------------- WEATHER ----------------

@app.get("/api/weather")
def weather(lat: float, lon: float):

    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "apparent_temperature,"
            "precipitation,"
            "wind_speed_10m,"
            "weather_code"
        ),
        "timezone": "auto"
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        return {
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "timezone": data.get("timezone"),
            "current": data.get("current", {})
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=503,
            detail="Weather service unavailable"
        )


# ---------------- AQI ----------------

@app.get("/api/aqi")
def aqi(lat: float, lon: float):

    url = "https://air-quality-api.open-meteo.com/v1/air-quality"

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "european_aqi,pm2_5,pm10",
        "timezone": "auto"
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        return {
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "current": data.get("current", {})
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=503,
            detail="AQI service unavailable"
        )


# ---------------- ROUTING ----------------

@app.get("/api/route")
def route(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float
):

    coordinates = (
        f"{start_lon},{start_lat};"
        f"{end_lon},{end_lat}"
    )

    url = (
        "https://router.project-osrm.org/"
        f"route/v1/driving/{coordinates}"
    )

    params = {
        "alternatives": "true",
        "steps": "true",
        "geometries": "geojson",
        "overview": "full"
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        if data.get("code") != "Ok":
            raise HTTPException(
                status_code=400,
                detail="Route not found"
            )

        routes = []

        for r in data.get("routes", []):

            routes.append({
                "distance_km": round(
                    r["distance"] / 1000,
                    2
                ),
                "duration_min": round(
                    r["duration"] / 60,
                    1
                ),
                "geometry": r["geometry"]
            })

        return {
            "routes": routes
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=503,
            detail="Routing service unavailable"
        )


# ---------------- CARBON ENGINE ----------------

def calculate_carbon(
    distance_km,
    weight,
    vehicle,
    vehicle_age,
    slope_percent=0,
    traffic_factor=1.0,
    weather_factor=1.0,
    aqi=50
):

    base_emission = {
        "petrol_car": 0.192,
        "diesel_van": 0.25,
        "electric_van": 0.06,
        "bike": 0.04
    }

    base = base_emission.get(vehicle, 0.20)

    payload_factor = 1 + (weight * 0.005)

    age_factor = 1 + max(0, vehicle_age - 3) * 0.015

    slope_factor = 1 + max(0, slope_percent) * 0.025

    aqi_factor = 1 + max(0, aqi - 100) * 0.0005

    emission = (
        distance_km
        * base
        * payload_factor
        * age_factor
        * slope_factor
        * traffic_factor
        * weather_factor
        * aqi_factor
    )

    return round(emission, 3)


@app.post("/api/carbon")
def carbon(req: CarbonRequest):

    value = calculate_carbon(
        req.distance_km,
        req.weight,
        req.vehicle,
        req.vehicle_age,
        req.slope_percent,
        req.traffic_factor,
        req.weather_factor,
        req.aqi
    )

    return {
        "carbon_kg": value,
        "confidence": 0.87
    }


# ---------------- ECO ROUTE ----------------

@app.post("/api/eco-route")
def eco_route(req: RouteRequest):

    coordinates = (
        f"{req.start_lon},{req.start_lat};"
        f"{req.end_lon},{req.end_lat}"
    )

    url = (
        "https://router.project-osrm.org/"
        f"route/v1/driving/{coordinates}"
    )

    params = {
        "alternatives": "true",
        "steps": "false",
        "geometries": "geojson",
        "overview": "full"
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        if data.get("code") != "Ok":
            raise HTTPException(
                status_code=400,
                detail="Route unavailable"
            )

        scored = []

        for index, r in enumerate(data["routes"]):

            distance = r["distance"] / 1000
            duration = r["duration"] / 60

            carbon = calculate_carbon(
                distance,
                req.weight,
                req.vehicle,
                req.vehicle_age
            )

            # Small time penalty so an extremely slow route
            # isn't always selected.
            score = carbon + (duration * 0.002)

            scored.append({
                "route_index": index + 1,
                "distance_km": round(distance, 2),
                "duration_min": round(duration, 1),
                "carbon_kg": carbon,
                "score": round(score, 4),
                "geometry": r["geometry"]
            })

        scored.sort(key=lambda x: x["score"])

        best = scored[0]

        conn = sqlite3.connect(DB_PATH)

        conn.execute("""
            INSERT INTO deliveries
            (pickup, destination, weight, vehicle,
             vehicle_age, distance_km, duration_min,
             carbon_kg, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "Selected coordinates",
            "Selected coordinates",
            req.weight,
            req.vehicle,
            req.vehicle_age,
            best["distance_km"],
            best["duration_min"],
            best["carbon_kg"],
            datetime.utcnow().isoformat()
        ))

        conn.commit()
        conn.close()

        return {
            "recommended": best,
            "alternatives": scored
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=503,
            detail="Routing service unavailable"
        )


# ---------------- GPS LIVE TRACKING ----------------

@app.websocket("/ws/location")
async def websocket_location(websocket: WebSocket):

    await websocket.accept()
    active_connections.add(websocket)

    try:

        while True:

            message = await websocket.receive_text()

            data = json.loads(message)

            lat = float(data["lat"])
            lon = float(data["lon"])

            conn = sqlite3.connect(DB_PATH)

            conn.execute("""
                INSERT INTO gps_logs
                (latitude, longitude, timestamp)
                VALUES (?, ?, ?)
            """, (
                lat,
                lon,
                datetime.utcnow().isoformat()
            ))

            conn.commit()
            conn.close()

            broadcast = {
                "type": "driver_location",
                "lat": lat,
                "lon": lon,
                "timestamp": datetime.utcnow().isoformat()
            }

            dead = []

            for connection in active_connections:

                try:
                    await connection.send_json(broadcast)
                except Exception:
                    dead.append(connection)

            for connection in dead:
                active_connections.discard(connection)

    except WebSocketDisconnect:
        active_connections.discard(websocket)

    except Exception:
        active_connections.discard(websocket)


# ---------------- DASHBOARD STATS ----------------

@app.get("/api/stats")
def stats():

    conn = sqlite3.connect(DB_PATH)

    deliveries = conn.execute(
        "SELECT COUNT(*) FROM deliveries"
    ).fetchone()[0]

    carbon = conn.execute(
        "SELECT COALESCE(SUM(carbon_kg),0) FROM deliveries"
    ).fetchone()[0]

    gps = conn.execute(
        "SELECT COUNT(*) FROM gps_logs"
    ).fetchone()[0]

    conn.close()

    return {
        "deliveries": deliveries,
        "carbon_kg": round(carbon, 3),
        "gps_points": gps
    }
