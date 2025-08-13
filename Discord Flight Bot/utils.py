import os
import discord
import random
from datetime import datetime, timedelta,UTC

from dotenv import load_dotenv
from serpapi import GoogleSearch
from supabase import create_client

# Load .env vars
load_dotenv()
SERPA_API_KEY = os.getenv("SERPA_AP_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Init Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


async def fetch_flights(params: dict, max_price: int | None = None):
    search = GoogleSearch(params)
    results = search.get_dict()

    flights_raw = results.get("best_flights", []) + results.get("other_flights", [])
    g_url = results.get("search_metadata", {}).get("google_flights_url", "")

    if max_price is None:
        matching = flights_raw
    else:
        matching = [f for f in flights_raw if f.get("price", float("inf")) <= max_price]

    return matching, flights_raw, g_url

async def fetch_roundtrip_flight(ctx, params: dict, max_price: int): 
    try: 
        search = GoogleSearch(params)
        results = search.get_dict()
        flights_raw = results.get("best_flights", []) + results.get("other_flights", [])

        if not flights_raw:
            await ctx.send("❌ No flights found.")
            return

        # Filter by user price threshold
        matching_flights = [f for f in flights_raw if f.get("price", float("inf")) <= max_price]

        g_url = results.get("search_metadata", {}).get("google_flights_url", "")
        return matching_flights, flights_raw, g_url
    except Exception as e:
        await ctx.send(f"⚠️ API error: {e}")

async def add_flight_info_to_supabase(ctx, departure_id: str, arrival_id: str, outbound_date: str, return_date: str, max_price: int): 
    # Save request for future weekly alerting
        supabase.table("FlightTracking").insert({
        "user_id": str(ctx.author.id),
        "departure_id": departure_id.upper(),
        "arrival_id": arrival_id.upper(),
        "outbound_date": outbound_date,
        "return_date": return_date,
        "max_price": max_price,
        "alert_sent": False,
        "last_checked": datetime.now(UTC).isoformat()
        }).execute()
        return

async def fetch_cheapest_oneway_flight(departure_id, arrival_id, type_of_flight = 2):
    future_date = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    params = {
        "engine": "google_flights",
        "departure_id": departure_id,
        "arrival_id": arrival_id,
        "outbound_date": future_date,
        "currency": "USD",
        "type": type_of_flight,
        "hl": "en",
        "api_key": SERPA_API_KEY
    }

    try:
        search = GoogleSearch(params)
        results = search.get_dict()

        print(f"🔧 Raw result for {departure_id} → {arrival_id}:", results)

        flights_raw = results.get("best_flights", []) + results.get("other_flights", [])
        if not flights_raw:
            print("⚠️ No best_flights or other_flights returned.")
            return [], None  # Returning empty list and no URL

        top_flights = []
        for flight_entry in flights_raw[:1]:
            try:
                segments = flight_entry.get("flights", [])
                if not segments:
                    continue

                first_leg = segments[0]
                flight_summary = {
                    "airline": first_leg.get("airline", "Unknown"),
                    "flight_number": first_leg.get("flight_number", "N/A"),
                    "departure": first_leg.get("departure_airport", {}).get("id", "Unknown"),
                    "arrival": first_leg.get("arrival_airport", {}).get("id", "Unknown"),
                    "departure_time": first_leg.get("departure_airport", {}).get("time", "N/A"),
                    "arrival_time": first_leg.get("arrival_airport", {}).get("time", "N/A"),
                    "price": flight_entry.get("price", "N/A"),
                    "duration": flight_entry.get("total_duration", "N/A"),
                    "url": results.get("search_metadata", {}).get("google_flights_url", "")
                }

                top_flights.append(flight_summary)

            except Exception as parse_err:
                print(f"❌ Failed to parse a flight entry: {parse_err}")
                continue

        return top_flights
    except Exception as e:
        print(f"❗️ Error fetching flights for {departure_id} → {arrival_id}: {e}")
        return None

async def fetch_user_home_airport(ctx, user_id): 
    # Fetch user's home airport
    result = (
        supabase.table("UserSetting")
        .select("home_airport")
        .eq("user_id", user_id)
        .execute()
        .data
    )

    if not result or not result[0].get("home_airport"):
        await ctx.send("✈️ You still need to set a hometown airport. Please run `!set_home` with your desired IATA code.")
        return None

    else: 
        home_airport = result[0]["home_airport"]
        return home_airport
    
def pick_random_destination(): 
    regions = {
        "Asia": ['SIN', 'ICN', 'HND'],
        "Europe": ['LHR', 'CDG', 'FCO'],
        "Americas": ['LAX', 'JFK', 'DEN']
    }

    region_name, destination_list = random.choice(list(regions.items()))
    dest = random.choice(destination_list)

    return region_name, dest

def cached_deal_destination(region_name, dest):
    # Check if we have a cached entry for this route in the past 24h
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    cache_response = (
        supabase.table("TodaysDeals")
        .select("*")
        .eq("region", region_name)
        .eq("airport_code", dest)
        .gte("created_at", cutoff)
        .limit(1)
        .execute()
    )

    print(cache_response)
    if cache_response: 
        return cache_response
    else:
        return None

def fetch_flights_from_serpapi(params):
    try:
        search = GoogleSearch(params)
        results = search.get_dict()
        flights_raw = results.get("best_flights", []) + results.get("other_flights", [])
        g_url = results.get("search_metadata", {}).get("google_flights_url", "")
        return flights_raw, g_url
    except Exception as e:
        print(f"❗️ API error: {e}")
        return [], ""
    
def pick_one(user_rows):
    return sorted(
        user_rows,
        key=lambda r: (
            r.get("last_checked") or "1970-01-01T00:00:00Z",
            r.get("outbound_date") or "9999-12-31"
        )
    )[0]