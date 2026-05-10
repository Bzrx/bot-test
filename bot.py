import discord
from discord.ext import commands
import asyncio
import os
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
from pymongo import MongoClient

# ---------------- LOAD ENV ----------------

load_dotenv()

TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

if not TOKEN:
    raise ValueError(
        "❌ TOKEN environment variable is missing.\n"
        "Add TOKEN in Render Environment Variables or .env file."
    )

if not MONGO_URI:
    raise ValueError(
        "❌ MONGO_URI environment variable is missing."
    )

# ---------------- MONGODB ----------------

client = MongoClient(MONGO_URI)

db = client["pluto_bot"]

wallets_collection = db["wallets"]
balances_collection = db["balances"]
ledger_collection = db["ledger"]

# ---------------- KEEP ALIVE ----------------

app = Flask('')


@app.route('/')
def home():
    return "Bot is alive!"


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()


# ---------------- INTENTS ----------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- CONFIG ----------------

BUYER_ROLE_NAME = "Buyer"
SUPPLIER_ROLE_NAME = "Supplier"
TICKET_CATEGORY_NAME = "Tickets"

BOT_COMMAND_CHANNEL = "bot-commands"

# ---------------- STORAGE ----------------

active_orders = {}
order_locks = {}

# ---------------- LOCK SYSTEM ----------------

def get_lock(order_id):
    if order_id not in order_locks:
        order_locks[order_id] = asyncio.Lock()

    return order_locks[order_id]


# ---------------- MONEY ----------------

def parse_amount(value):
    value = value.lower().replace(",", "").strip()

    if value.endswith("b"):
        return int(float(value[:-1]) * 1_000_000_000)

    if value.endswith("m"):
        return int(float(value[:-1]) * 1_000_000)

    return int(value)


def format_amount(value):
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}b".replace(".0", "")

    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m".replace(".0", "")

    return str(value)


# ---------------- ACCEPT MODAL ----------------

class AcceptModal(discord.ui.Modal, title="Accept Order"):

    sell_amount = discord.ui.TextInput(
        label="How much are you selling?",
        placeholder="Example: 100m",
        required=True,
        max_length=20
    )

    def __init__(self, order_id, supplier):
        super().__init__()
        self.order_id = order_id
        self.supplier = supplier

    async def on_submit(self, interaction: discord.Interaction):

        order = active_orders.get(self.order_id)

        if not order:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True
            )
            return

        lock = get_lock(self.order_id)

        async with lock:

            try:
                sell_value = parse_amount(self.sell_amount.value)

            except:
                await interaction.response.send_message(
                    "❌ Invalid amount.",
                    ephemeral=True
                )
                return

            remaining = order["remaining"]

            if order.get("exact"):

                if sell_value != remaining:
                    await interaction.response.send_message(
                        f"❌ You must accept exactly {format_amount(remaining)}",
                        ephemeral=True
                    )
                    return

            else:

                if sell_value <= 0 or sell_value > remaining:
                    await interaction.response.send_message(
                        "❌ Invalid amount.",
                        ephemeral=True
                    )
                    return

            guild = order["guild"]
            buyer = order["buyer"]
            supplier = self.supplier

            category = discord.utils.get(
                guild.categories,
                name=TICKET_CATEGORY_NAME
            )

            if not category:
                category = await guild.create_category(
                    TICKET_CATEGORY_NAME
                )

            ticket_name = supplier.name.lower().replace(" ", "-")

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    read_messages=False
                ),

                buyer: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True
                ),

                supplier: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True
                ),

                guild.me: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True
                )
            }

            channel = await guild.create_text_channel(
                name=ticket_name,
                category=category,
                overwrites=overwrites
            )

            order["remaining"] -= sell_value
            remaining_after = order["remaining"]

            order["last_sell_amount"] = sell_value
            order["last_supplier"] = supplier
            order["ticket_channel_id"] = channel.id

            for msg in order.get("messages", []):

                try:

                    if remaining_after <= 0:

                        await msg.edit(
                            content=(
                                f"❌ ORDER FILLED\n\n"
                                f"Original Amount: "
                                f"{order['original_amount']}\n"
                                f"Rate: {order['rate']} PHP"
                            ),
                            view=None
                        )

                    else:

                        await msg.edit(
                            content=(
                                f"📢 **NEW ORDER**\n\n"
                                f"💰 Remaining: "
                                f"{format_amount(remaining_after)}\n"
                                f"💵 Rate: {order['rate']} PHP"
                            ),
                            view=AcceptView(self.order_id)
                        )

                except:
                    pass

            await channel.send(
                f"🎫 **ORDER STARTED**\n\n"
                f"👤 Buyer: {buyer.mention}\n"
                f"🛒 Supplier: {supplier.mention}\n\n"
                f"💰 Selling: {format_amount(sell_value)}\n"
                f"💵 Rate: {order['rate']} PHP\n\n"
                f"Remaining Order: "
                f"{format_amount(remaining_after)}\n\n"
                f"Buyer confirms with !confirm"
            )

            await interaction.response.send_message(
                "✅ Ticket created.",
                ephemeral=True
            )


# ---------------- ACCEPT BUTTON ----------------

class AcceptView(discord.ui.View):

    def __init__(self, order_id):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(
        label="Accept Order",
        style=discord.ButtonStyle.green
    )
    async def accept(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        order = active_orders.get(self.order_id)

        if not order:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True
            )
            return

        if order["remaining"] <= 0:
            await interaction.response.send_message(
                "❌ This order is already filled.",
                ephemeral=True
            )
            return

        guild = order["guild"]

        member = guild.get_member(interaction.user.id)

        role = discord.utils.get(
            guild.roles,
            name=SUPPLIER_ROLE_NAME
        )

        if not member:
            await interaction.response.send_message(
                "❌ You are not in the server.",
                ephemeral=True
            )
            return

        if role not in member.roles:
            await interaction.response.send_message(
                "❌ You are not a supplier.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            AcceptModal(self.order_id, member)
        )


# ---------------- CASHOUT CONFIRM BUTTON ----------------

class CashoutConfirmView(discord.ui.View):

    def __init__(self, supplier, amount):
        super().__init__(timeout=None)
        self.supplier = supplier
        self.amount = amount

    @discord.ui.button(
        label="Confirm Sent",
        style=discord.ButtonStyle.green
    )
    async def confirm_sent(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        button.disabled = True

        balances_collection.update_one(
            {"user_id": str(self.supplier.id)},
            {"$set": {"balance": 0}},
            upsert=True
        )

        ledger_collection.update_one(
            {"user_id": str(self.supplier.id)},
            {"$set": {"entries": []}},
            upsert=True
        )

        try:
            await self.supplier.send(
                f"✅ Your cashout of ₱{self.amount:,.2f} "
                f"has been confirmed as sent by "
                f"{interaction.user.mention}."
            )

        except:
            pass

        await interaction.response.edit_message(view=self)

        await interaction.followup.send(
            "✅ Cashout marked as sent.",
            ephemeral=True
        )


# ---------------- NEED ----------------

@bot.command()
async def need(ctx, amount: str, rate: str):

    buyer_role = discord.utils.get(
        ctx.guild.roles,
        name=BUYER_ROLE_NAME
    )

    supplier_role = discord.utils.get(
        ctx.guild.roles,
        name=SUPPLIER_ROLE_NAME
    )

    if not buyer_role or buyer_role not in ctx.author.roles:
        await ctx.send("❌ Only Buyer role can use !need.")
        return

    if not supplier_role:
        await ctx.send("❌ Supplier role not found.")
        return

    try:
        amount_value = parse_amount(amount)

    except:
        await ctx.send("❌ Invalid amount.")
        return

    order_id = str(ctx.message.id)

    active_orders[order_id] = {
        "remaining": amount_value,
        "original_amount": amount,
        "rate": rate,
        "buyer": ctx.author,
        "guild": ctx.guild,
        "messages": [],
        "exact": False
    }

    view = AcceptView(order_id)

    sent = 0

    for member in supplier_role.members:

        if member.bot:
            continue

        try:

            msg = await member.send(
                f"📢 **NEW ORDER**\n\n"
                f"💰 Remaining: {amount}\n"
                f"💵 Rate: {rate} PHP",
                view=view
            )

            active_orders[order_id]["messages"].append(msg)

            sent += 1

        except:
            pass

    await ctx.send(
        f"✅ Sent to {sent} suppliers."
    )


# ---------------- NEED EXACT ----------------

@bot.command()
async def needexact(ctx, amount: str, rate: str):

    buyer_role = discord.utils.get(
        ctx.guild.roles,
        name=BUYER_ROLE_NAME
    )

    supplier_role = discord.utils.get(
        ctx.guild.roles,
        name=SUPPLIER_ROLE_NAME
    )

    if not buyer_role or buyer_role not in ctx.author.roles:
        await ctx.send("❌ Only Buyer role can use !needexact.")
        return

    if not supplier_role:
        await ctx.send("❌ Supplier role not found.")
        return

    try:
        amount_value = parse_amount(amount)

    except:
        await ctx.send("❌ Invalid amount.")
        return

    order_id = str(ctx.message.id)

    active_orders[order_id] = {
        "remaining": amount_value,
        "original_amount": amount,
        "rate": rate,
        "buyer": ctx.author,
        "guild": ctx.guild,
        "messages": [],
        "exact": True
    }

    view = AcceptView(order_id)

    sent = 0

    for member in supplier_role.members:

        if member.bot:
            continue

        try:

            msg = await member.send(
                f"📢 **NEW EXACT ORDER**\n\n"
                f"💰 Remaining: {amount}\n"
                f"💵 Rate: {rate} PHP\n\n"
                f"⚠️ Must be accepted fully.",
                view=view
            )

            active_orders[order_id]["messages"].append(msg)

            sent += 1

        except:
            pass

    await ctx.send(
        f"✅ Exact order sent to {sent} suppliers."
    )


# ---------------- WALLET ----------------

@bot.command()
async def wallet(ctx, action=None, method=None, *, value=None):

    target = (
        ctx.message.mentions[0]
        if ctx.message.mentions
        else ctx.author
    )

    wallets = wallets_collection.find_one(
        {"user_id": str(target.id)}
    )

    if action is None and target == ctx.author:

        if not wallets:
            await ctx.send("❌ No wallet saved.")
            return

        ltc = wallets.get("ltc", "Not set")
        gcash = wallets.get("gcash", "Not set")

        await ctx.send(
            f"💼 Your Wallets\n"
            f"**LTC:** {ltc}\n"
            f"**GCash:** {gcash}"
        )
        return

    if ctx.message.mentions:

        if not wallets:
            await ctx.send(
                f"❌ {target.mention} has no wallet saved."
            )
            return

        ltc = wallets.get("ltc", "Not set")
        gcash = wallets.get("gcash", "Not set")

        await ctx.send(
            f"💼 Wallets of {target.mention}\n"
            f"**LTC:** {ltc}\n"
            f"**GCash:** {gcash}"
        )
        return

    if action != "set":
        await ctx.send(
            "Usage:\n"
            "!wallet\n"
            "!wallet set ltc ADDRESS\n"
            "!wallet set gcash NUMBER"
        )
        return

    if method not in ["ltc", "gcash"]:
        await ctx.send(
            "❌ Only ltc and gcash are allowed."
        )
        return

    if not value:
        await ctx.send("❌ Please provide a value.")
        return

    wallets_collection.update_one(
        {"user_id": str(ctx.author.id)},
        {
            "$set": {
                method: value
            }
        },
        upsert=True
    )

    await ctx.send(
        f"✅ {method.upper()} wallet saved."
    )


# ---------------- BALANCE ----------------

@bot.command()
async def balance(ctx):

    target = (
        ctx.message.mentions[0]
        if ctx.message.mentions
        else ctx.author
    )

    data = balances_collection.find_one(
        {"user_id": str(target.id)}
    )

    balance_value = 0

    if data:
        balance_value = data.get("balance", 0)

    await ctx.send(
        f"💰 {target.mention}'s balance: "
        f"₱{balance_value:,.2f}"
    )


# ---------------- CONFIRM ----------------

@bot.command()
async def confirm(ctx):

    order = None

    for data in active_orders.values():

        if (
            data["buyer"].id == ctx.author.id
            and data.get("ticket_channel_id") == ctx.channel.id
        ):
            order = data
            break

    if not order:
        await ctx.send(
            "❌ No active order found in this ticket."
        )
        return

    supplier = order.get("last_supplier")

    sold_amount = order.get("last_sell_amount", 0)

    if not supplier:
        await ctx.send("❌ Supplier not found.")
        return

    credited = (
        (sold_amount / 1_000_000)
        * float(order["rate"])
    )

    old_balance = balances_collection.find_one(
        {"user_id": str(supplier.id)}
    )

    current_balance = 0

    if old_balance:
        current_balance = old_balance.get("balance", 0)

    balances_collection.update_one(
        {"user_id": str(supplier.id)},
        {
            "$set": {
                "balance": current_balance + credited
            }
        },
        upsert=True
    )

    ledger = ledger_collection.find_one(
        {"user_id": str(supplier.id)}
    )

    entries = []

    if ledger:
        entries = ledger.get("entries", [])

    entries.append({
        "buyer_id": ctx.author.id,
        "amount": credited
    })

    ledger_collection.update_one(
        {"user_id": str(supplier.id)},
        {"$set": {"entries": entries}},
        upsert=True
    )

    await ctx.send(
        f"✅ Confirmed.\n"
        f"{supplier.mention} received "
        f"₱{credited:,.2f}"
    )


# ---------------- CASHOUT ----------------

@bot.command()
async def cashout(ctx, method=None):

    if method is None:
        await ctx.send(
            "❌ Usage: !cashout gcash or !cashout ltc"
        )
        return

    method = method.lower()

    if method not in ["gcash", "ltc"]:
        await ctx.send(
            "❌ Only gcash or ltc are allowed."
        )
        return

    ledger = ledger_collection.find_one(
        {"user_id": str(ctx.author.id)}
    )

    entries = []

    if ledger:
        entries = ledger.get("entries", [])

    if not entries:
        await ctx.send(
            "❌ You have no cashout balance."
        )
        return

    wallets = wallets_collection.find_one(
        {"user_id": str(ctx.author.id)}
    )

    if not wallets:
        await ctx.send(
            "❌ No wallet found."
        )
        return

    wallet_value = wallets.get(method)

    if not wallet_value:
        await ctx.send(
            f"❌ You do not have a "
            f"{method.upper()} wallet set."
        )
        return

    grouped = {}

    for entry in entries:

        buyer = ctx.guild.get_member(
            entry["buyer_id"]
        )

        if not buyer:
            continue

        if buyer.id not in grouped:
            grouped[buyer.id] = {
                "buyer": buyer,
                "amount": 0
            }

        grouped[buyer.id]["amount"] += entry["amount"]

    sent = 0

    for data in grouped.values():

        buyer = data["buyer"]
        amount = data["amount"]

        try:

            view = CashoutConfirmView(
                ctx.author,
                amount
            )

            await buyer.send(
                f"💸 **CASHOUT REQUEST**\n\n"
                f"Supplier: {ctx.author.mention}\n"
                f"Amount: ₱{amount:,.2f}\n"
                f"Method: {method.upper()}\n\n"
                f"Wallet:\n{wallet_value}\n\n"
                f"Press the button below after sending "
                f"the payment.",
                view=view
            )

            sent += 1

        except:
            pass

    await ctx.send(
        f"✅ Cashout request sent "
        f"to {sent} buyer(s)."
    )


# ---------------- MESSAGE FILTER ----------------

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    if (
        hasattr(message.channel, "name")
        and message.channel.name == BOT_COMMAND_CHANNEL
    ):

        if not message.content.startswith("!"):
            try:
                await message.delete()
            except:
                pass

            return

        await bot.process_commands(message)

        try:
            await message.delete()
        except:
            pass

        return

    await bot.process_commands(message)


# ---------------- ERROR HANDLER ----------------

@bot.event
async def on_command_error(ctx, error):
    print(error)

    try:
        await ctx.send(f"❌ {error}")
    except:
        pass


# ---------------- READY ----------------

@bot.event
async def on_ready():

    print("=" * 50)
    print(f"✅ Logged in as {bot.user}")
    print("=" * 50)


# ---------------- START ----------------

keep_alive()

try:
    bot.run(TOKEN)

except discord.LoginFailure:
    print("❌ Invalid Discord Token")

except Exception as e:
    print(f"❌ Bot crashed: {e}")
