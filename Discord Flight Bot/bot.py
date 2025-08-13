import os
import discord
import random
from utils import fetch_roundtrip_flight, fetch_cheapest_oneway_flight, add_flight_info_to_supabase, fetch_user_home_airport, pick_random_destination, cached_deal_destination, fetch_flights
from datetime import date,datetime, timedelta, timezone as tz
from discord.ext import commands, tasks
from dotenv import load_dotenv
from supabase import create_client
from serpapi import GoogleSearch
from collections import defaultdict


# Load .env vars
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SERPA_API_KEY = os.getenv("SERPA_AP_KEY")
ALERTS_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

# Init Supabase
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True  # Required to read message contents
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)
UTC = tz.utc  # if you already have UTC imported, keep yours

# Turning the bot on 
@bot.event
async def on_ready():
    print(f"✅ Bot {bot.user} is now running!")
    if not run_weekly_alerts.is_running():
        run_weekly_alerts.start()

'''
Need to finish this function to send alerts based on if there are flights now in the users picked max price range
'''
@tasks.loop(hours=168)
async def run_weekly_alerts():
    channel = bot.get_channel(ALERTS_CHANNEL_ID)
    if channel is None:
        print("⚠️ ALERTS_CHANNEL_ID not found or bot lacks permission.")
        return

    # Pull all candidates that still need alerts
    resp = supabase.table("FlightTracking").select("*").eq("alert_sent", False).execute()
    data = resp.data or []
    if not data:
        print("ℹ️ No rows to check this run.")
        return

    # Group by user_id and pick ONE row per user (oldest last_checked, then earliest outbound_date)
    by_user = defaultdict(list)
    for r in data:
        by_user[r["user_id"]].append(r)

    def pick_one(rows):
        return sorted(
            rows,
            key=lambda r: (
                r.get("last_checked") or "1970-01-01T00:00:00Z",
                r.get("outbound_date") or "9999-12-31",
            )
        )[0]

    rows_one_per_user = [pick_one(rows) for rows in by_user.values()]

    # Build a route cache so we only call SerpAPI once per (dep, arr, trip_length_days)
    route_cache = {}  # key -> (matching_flights, flights_raw, g_url)
    rows_one_per_user = rows_one_per_user[:1]
    for first_row in rows_one_per_user:
        # --- your original block starts here (kept as intact as possible) ---

        departure_id = first_row["departure_id"]
        arrival_id = first_row["arrival_id"]
        max_price = first_row["max_price"]

        outbound = datetime.fromisoformat(first_row["outbound_date"])
        return_  = datetime.fromisoformat(first_row["return_date"])
        original_length_of_vacation = (return_ - outbound).days

        # Calculate the new dates for the params we will pass
        todays_date = date.today()
        new_return_date = todays_date + timedelta(days=original_length_of_vacation)

        params = {
            "engine": "google_flights",
            "departure_id": departure_id,
            "arrival_id":   arrival_id,
            "outbound_date": todays_date.strftime("%Y-%m-%d"),
            "return_date":   new_return_date.strftime("%Y-%m-%d"),
            "currency": "USD",
            "hl": "en",
            "api_key": SERPA_API_KEY
        }

        # --- end of your param build ---

        # 3a) Dedupe: use a key so multiple users on same route/length reuse one API call
        route_key = (departure_id, arrival_id, original_length_of_vacation)

        if route_key not in route_cache:
            # one real API call for this route/length
            matching_flights, flights_raw, g_url = await fetch_flights(params, max_price=None)
            route_cache[route_key] = (matching_flights, flights_raw, g_url)

        # Pull cached result and apply the user’s max price filter locally
        matching_flights, flights_raw, g_url = route_cache[route_key]
        matching_flights_for_user = [f for f in (matching_flights or []) if f.get("price", 10**9) <= max_price]

        # Always update last_checked
        supabase.table("FlightTracking").update({
            "last_checked": datetime.now(UTC).isoformat()
        }).eq("id", first_row["id"]).execute()

        # If nothing under price for this user, skip sending
        if not matching_flights_for_user:
            continue

        # --- your embed build & send (kept, minimal edits) ---
        embed = discord.Embed(
            title=f"🎯 Great news we found flights within your price of ${max_price} from {departure_id} → {arrival_id}",
            description=f"Found {len(matching_flights_for_user)} flights under your threshold:",
            color=discord.Color.green()
        )

        for flight_data in matching_flights_for_user[:3]:
            flight_segments = []
            for leg in flight_data.get("flights", []):
                airline = leg.get("airline", "Unknown Airline")
                flight_number = leg.get("flight_number", "N/A")
                dep = leg.get("departure_airport", {})
                arr = leg.get("arrival_airport", {})
                flight_segments.append(
                    f"✈️ **{airline} {flight_number}**\n"
                    f"{dep.get('name')} ({dep.get('id')}) → {arr.get('name')} ({arr.get('id')})\n"
                    f"🕒 {dep.get('time')} → {arr.get('time')}"
                )

            segment_text = "\n\n".join(flight_segments)
            embed.add_field(
                name=f"💵 ${flight_data.get('price', '?')} | 🧭 Duration: {flight_data.get('total_duration')} min",
                value=segment_text,
                inline=False
            )

        if g_url:
            embed.add_field(name="🔗 View on Google Flights", value=f"[Open Link]({g_url})", inline=False)

        embed.set_footer(text="Powered by SerpAPI + Google Flights")
        await channel.send(embed=embed)

        # Optional: mark as alerted so you don’t spam next run
        supabase.table("FlightTracking").update({
            "alert_sent": True
        }).eq("id", first_row["id"]).execute()

'''
Function to set the default home airport for a specific user
'''
@bot.command(name = "set_home")
async def add_home_airport(ctx, home_airport: str): 
    user_id = str(ctx.author.id)
    
    #Adding the home airport into the UserSettings table along with the ability for it to be updated
    supabase.table("UserSetting").upsert({
        "user_id": user_id,
        "home_airport": home_airport.upper()
    }, on_conflict=["user_id"]).execute()

    await ctx.send(f"Your hometown airport has been updated to `{home_airport.upper()}`.")

'''
Main function to lookup flights, if a user tries to lookup a flight with a max price and there is nothing
that meets that criteria at the time then the flight is added to the database and the user is shown the 
best flights currently available
'''
@bot.command(name = "lookup_flight")
async def search_flight(ctx, departure_id: str, arrival_id: str, outbound_date: str, return_date: str, max_price: int):
    await ctx.send(f"🔍 Searching for flights from `{departure_id}` to `{arrival_id}` under **${max_price}**...")

    params = {
        "engine": "google_flights",
        "departure_id": departure_id.upper(),
        "arrival_id": arrival_id.upper(),
        "outbound_date": outbound_date,
        "return_date": return_date,
        "currency": "USD",
        "hl": "en",
        "api_key": SERPA_API_KEY
    }

    try:
        matching_flights, flights_raw, flights_url = await fetch_roundtrip_flight(ctx, params, max_price)

        # If no flights match, save to Supabase and show best price
        if not matching_flights:
            await add_flight_info_to_supabase(ctx, departure_id, arrival_id, outbound_date, return_date, max_price)

            best = flights_raw[0]
            matching_flights = [best]

            embed = discord.Embed(
                title="💡 No flights under your price range",
                description="We've saved your search and will check again weekly. Here's the current best flights:",
                color=discord.Color.orange()
            )
        else:
            embed = discord.Embed(
                title=f"🎯 Flights Under ${max_price}",
                description=f"Found {len(matching_flights)} flights under your threshold:",
                color=discord.Color.green()
            )

        # Display up to 3 flights
        for flight_data in matching_flights[:3]:
            flight_segments = []
            for leg in flight_data.get("flights", []):
                airline = leg.get("airline", "Unknown Airline")
                flight_number = leg.get("flight_number", "N/A")
                dep = leg.get("departure_airport", {})
                arr = leg.get("arrival_airport", {})
                flight_segments.append(
                    f"✈️ **{airline} {flight_number}**\n"
                    f"{dep.get('name')} ({dep.get('id')}) → {arr.get('name')} ({arr.get('id')})\n"
                    f"🕒 {dep.get('time')} → {arr.get('time')}"
                )

            segment_text = "\n\n".join(flight_segments)
            embed.add_field(
                name=f"💵 ${flight_data.get('price', '?')} | 🧭 Duration: {flight_data.get('total_duration')} min",
                value=segment_text,
                inline=False
            )
        
        if flights_url:
            embed.add_field(name="🔗 View on Google Flights", value=f"[Open Link]({flights_url})", inline=False)

        embed.set_footer(text="Powered by SerpAPI + Google Flights")
        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"⚠️ API error: {e}")

'''
Command to show all of the flights that a user has searched that didn't 
meet their criteria and that they haven't been alerted about
'''
@bot.command(name = "my_flights")
async def flights_in_database(ctx):
    user_id = str(ctx.author.id)

    results = supabase.table("FlightTracking").select("*").eq("user_id", user_id).execute().data

    if not results: 
        await ctx.send("The current user doesn't have any saved flight price alerts")
    
    embed = discord.Embed(
        title = " Your Tracked Flight Alerts", 
        description = "These are the flights that are going to be checked for weekly updates.", 
        color = discord.Color.blue()
    )

    for row in results: 
        route =f"{row['departure_id']} -> {row['arrival_id']}"
        dates = f"{row['outbound_date']} -> {row['return_date']}"
        price = f"{row['max_price']}"
        embed.add_field(name=route, value=f" {dates} \n Max Price: {price}", inline = False)
    
    await ctx.send(embed = embed)

'''
7/24/2025
This function now works and stores the info into the database but it prints out 3 things instead of just one I need 
to work on the loop to make sure that it only prints out one. Also take out all of the debugging print statements
'''
@bot.command(name="todays_deals")
async def lookup_todays_flight_deals(ctx):
    user_id = str(ctx.author.id)

    region_name, dest = pick_random_destination()

    home_airport = await fetch_user_home_airport(ctx, user_id)

    if home_airport is None: 
        return

    await ctx.send(f"🔎 Finding today's best deals from `{home_airport}`...")

    embed = discord.Embed(
        title="🔥 Best Flight Deals Today",
        description="Here are some one-way options to popular destinations!",
        color=discord.Color.blue()
    )

    # If we already have relevant information used what is cached inside of our database
    cache_response = cached_deal_destination(region_name, dest)
    if cache_response.data:
        cached = cache_response.data[0]
        embed.add_field(
            name=f"{region_name} → {dest} | 💵 ${cached['price']}",
            value=f"📦 Pulled from cache (last 24 hrs)\n🔗 [Google Flights]({cached.get('url', 'https://www.google.com/travel/flights')})",
            inline=False
        )
    else:
    # 4. Fetch live if not cached
        top_flights = await fetch_cheapest_oneway_flight(home_airport, dest, 2)
        if not top_flights:
            await ctx.send("😔 No flights found right now. Try again later!")
            return

        for flight in top_flights:
            airline = flight.get("airline", "Unknown")
            flight_number = flight.get("flight_number", "N/A")
            departure = flight.get("departure", "N/A")
            arrival = flight.get("arrival", "N/A")
            departure_time = flight.get("departure_time", "N/A")
            arrival_time = flight.get("arrival_time", "N/A")
            price = flight.get("price", "N/A")
            duration = flight.get("duration", "N/A")
            url = flight.get("url", "https://www.google.com/travel/flights")

            embed.add_field(
                name=f"{region_name} → {dest} | 💵 ${price}",
                value=(
                    f"**{airline} {flight_number}**\n"
                    f"{departure} → {arrival}\n"
                    f"🕒 {departure_time} → {arrival_time} ({duration})\n"
                    f"🔗 [View on Google Flights]({url})"
                ),
                inline=False
            )

        # Save only the first (top) flight to cache
        top_flight = top_flights[0]
        supabase.table("TodaysDeals").insert({
            "region": region_name,
            "airport_code": dest,
            "price": top_flight.get("price"),
            "flight_url": top_flight.get("url", ""),
            "created_at": datetime.now(UTC).isoformat()
        }).execute()

    embed.set_footer(text="Built by Lindzi • Powered by SerpAPI + Google Flights")
    await ctx.send(embed=embed)

'''
Command to delete all of the flights from a specific user that have specific 
departure and arrival IATA codes that are saved inside of the database 
'''
@bot.command(name = "delete_flight")
async def delete_flight(ctx, departure_id: str, arrival_id: str): 
    user_id = str(ctx.author.id)

    try: 
        response = (
            supabase.table("FlightTracking")
            .delete()
            .eq("user_id", user_id)
            .eq("departure_id", departure_id)
            .eq("arrival_id", arrival_id)
            .execute()
        )

        if response.data:
            await ctx.send(f"All saved alerts for `{departure_id.upper()} → {arrival_id.upper()}` have been deleted.")
        else:
            await ctx.send(f"No saved alerts found for `{departure_id.upper()} → {arrival_id.upper()}`.")

    except Exception as e: 
        await ctx.send(f"Error deleting the flight alert: {e}")

'''
Command to show all of the available commands to a user
'''
@bot.command(name = "help")
async def help_command(ctx):
    embed = discord.Embed(
        title = " Flight Tracker Bot Commands",
        description = "Here's how to use the bot to track and manage flights:",
        color= discord.Color.blue()
    )
    embed.add_field(
        name="`!my_flights`", 
        value="List all your tracked flights and when they were added.",
        inline=False
    )
    embed.add_field(
        name="`!lookup_flight <from> <to> <outbound_date> <return_date> <max_price>`",
        value=(
            "Track a round-trip flight from one airport to another.\n"
            "**Example:** `!track_flight PEK AUS 2025-07-05 2025-07-11 600`\n\n"
            "**Required Inputs:**\n"
            "• `from` = departure airport IATA code (e.g., PEK)\n"
            "• `to` = arrival airport IATA code (e.g., AUS)\n"
            "• `outbound_date` = travel start date (YYYY-MM-DD)\n"
            "• `return_date` = return flight date (YYYY-MM-DD)\n"
            "• `max_price` = price threshold in USD"
        ),
        inline=False
    )   
    embed.add_field(
        name="!my_alerts",
        value = "This will show you which tracked flights have yet to be triggered",
        inline = False
    )
    embed.add_field(
        name="!todays_deals",
        value ="This will show you deals that are here today but might be gone tomorrow based on what you have set as your hometown", 
        inline = False
    )
    embed.add_field(
        name="!delete_flight <from> <to>", 
        value = "This will take out flights that you have routed to certain destinations no matter what the max price range was set at",
        inline = False
    )
    embed.add_field(
        name="!set_home <IATA>", 
        value= "Set your hometown airport so that you can see which deals are available to to you when running the deals today command",
        inline=False
    )
    embed.set_footer(text="Created by Lindzi")
    await ctx.send(embed = embed)


bot.run(DISCORD_TOKEN)
