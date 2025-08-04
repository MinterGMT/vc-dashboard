# ===============================================
# On-Chain VC Intelligence Dashboard
#
# Author: MinterGMT
# Project: vc-dashboard
#
# Description:
# A real-time dashboard that allows users to analyze the on-chain activity 
# and portfolio movements of venture capital firms.
#
# Features:
# - Select a VC for a deep dive, or select 'All VCs' for a market overview
# - View aggregated portfolio overview and wallet breakdown
# - Analyze token portfolio deep dive
# - View recent wallet activity
# - Generate network graph of recent transactions
#
# ===============================================

# --- IMPORTS ---
import os
import time
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
import requests
import plotly.express as px
from datetime import datetime
from pycoingecko import CoinGeckoAPI
import networkx as nx
import streamlit.components.v1 as components
from pyvis.network import Network

# --- 1. CONFIGURATION ---
load_dotenv()
DUNE_API_KEY = os.getenv("DUNE_API_KEY")
COVALENT_API_KEY = os.getenv("COVALENT_API_KEY")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

# The query ID for the pre-written Dune query that returns a list of VC wallets
DUNE_QUERY_ID = 5551211

# --- 2. HELPER & API FUNCTIONS ---
# These functions are used to get the data, clean it, and prepare it for display
def clean_firm_name(name):
    """
    Takes a raw wallet name from Dune (e.g., "a16z.eth") and returns a standardized,
    human-readable firm name (e.g., "a16z"). This is done for grouping wallets correctly 
    in the UI.
    """
    name_lower = name.lower()
    if 'a16z' in name_lower or 'andreessen' in name_lower: return 'a16z'
    if 'paradigm' in name_lower: return 'Paradigm'
    if 'dragonfly' in name_lower: return 'Dragonfly Capital'
    if 'coinbase' in name_lower: return 'Coinbase Ventures'
    if 'pantera' in name_lower: return 'Pantera Capital'
    return 'Other'

@st.cache_data(ttl=3600, show_spinner=False)
def get_dune_watchlist(query_id, api_key):
    """
    Executes a Dune Analytics query via their API.
    Handles the start of execution and polling for results. 
    The results are cached for 1 hour to improve app performance and avoid re-querying."""
    execution_url = f"https://api.dune.com/api/v1/query/{query_id}/execute"
    headers = {"X-DUNE-API-KEY": api_key}
    try:
        execution_response = requests.post(execution_url, headers=headers)
        execution_response.raise_for_status()
        execution_id = execution_response.json()['execution_id']
    except requests.exceptions.RequestException as e:
        st.error(f"Error starting Dune query: {e}"); return None
    result_url = f"https://api.dune.com/api/v1/execution/{execution_id}/results"
    while True:
        try:
            result_response = requests.get(result_url, headers=headers)
            result_response.raise_for_status()
            status_data = result_response.json()
            state = status_data.get('state')
            if state == 'QUERY_STATE_COMPLETED':
                df = pd.DataFrame(status_data['result']['rows'])
                df['Firm'] = df['name'].apply(clean_firm_name)
                return df
            elif state in ['QUERY_STATE_FAILED', 'QUERY_STATE_CANCELLED']:
                st.error(f"Dune query failed: {status_data.get('error', 'Unknown error')}"); return None
            time.sleep(3)
        except requests.exceptions.RequestException as e:
            st.error(f"Error fetching Dune results: {e}"); return None

def get_covalent_portfolio(wallet_address, api_key):
    """Fetches a VC wallet's complete token portfolio from the Covalent API."""
    url = f"https://api.covalenthq.com/v1/eth-mainnet/address/{wallet_address}/balances_v2/"
    params = {'key': api_key}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json().get('data', {})
        return data.get('items', [])
    except requests.exceptions.RequestException: return None

def get_token_transfers(wallet_address, api_key):
    """Fetches the 100 most recent ERC20 token transfers for a wallet from the Etherscan API."""
    params = {"module": "account", "action": "tokentx", "address": wallet_address, "sort": "desc", "apikey": api_key, "page": 1, "offset": 100}
    response = requests.get("https://api.etherscan.io/api", params=params)
    return response.json().get("result", []) if response.ok else []

def build_price_map_from_portfolio(token_df):
    """Creates a {symbol: price_per_token} map from a Covalent portfolio dataframe.
    This is more efficient for pricing transactions, as it uses pre-fetched data."""
    price_map = {}
    if token_df is not None and not token_df.empty:
        unique_tokens = token_df.drop_duplicates(subset=['contract_ticker_symbol'])
        for _, row in unique_tokens.iterrows():
            price_map[row['contract_ticker_symbol']] = row.get('quote_rate')
    return price_map

def get_address_label(address, lookup_df):
    """
    Checks if an address belongs to another known VC in the master list. 
    If not, it returns a shortened, human-readable version of the address.
    """
    match = lookup_df[lookup_df['address'].str.lower() == address.lower()]
    if not match.empty: return match.iloc[0]['name']
    return f"{address[:6]}...{address[-4:]}"

@st.cache_data(ttl=86400)
def get_coingecko_id_by_contract(contract_address):
    if not contract_address: return None
    cg = CoinGeckoAPI()
    try:
        info = cg.get_coin_info_from_contract_address_by_id(id='ethereum', contract_address=contract_address)
        return info.get('id')
    except Exception: return None

@st.cache_data(ttl=3600)
def get_historical_price(coingecko_id, date_str):
    """
    Gets the historical price of a token from CoinGecko.
    This is used to calculate the cost basis of a token when calculating P&L.
    """
    if not coingecko_id: return None
    cg = CoinGeckoAPI()
    try:
        data = cg.get_coin_history_by_id(id=coingecko_id, date=date_str, localization='false')
        if 'market_data' in data and 'current_price' in data['market_data'] and 'usd' in data['market_data']['current_price']:
            return data['market_data']['current_price']['usd']
    except Exception: return None
    return None

def calculate_unrealized_pnl(firm_wallets_df, api_key):
    st.info(f"Performing deep analysis on {len(firm_wallets_df)} wallets. This will be slow.")
    wallet_addresses = [addr.split('/')[-1] for addr in firm_wallets_df['Address'].tolist()]
    all_txs = []
    progress_bar = st.progress(0, "Fetching transaction history for all wallets...")
    for i, address in enumerate(wallet_addresses):
        progress_bar.progress((i + 1) / len(wallet_addresses), f"Fetching history for wallet {i+1}/{len(wallet_addresses)}...")
        all_txs.extend(get_token_transfers(address, api_key))
        time.sleep(0.5)
    progress_bar.empty()

    if not all_txs:
        st.warning("Could not retrieve any transaction history for this firm.")
        return pd.DataFrame()

    token_df = st.session_state.all_tokens_df[st.session_state.all_tokens_df['Firm'] == firm_wallets_df.iloc[0]['Firm']]
    agg_tokens = token_df.groupby('contract_ticker_symbol').agg(total_value=('quote', 'sum'), quote_rate=('quote_rate', 'first'))
    
    cost_basis_data = {}
    price_progress = st.progress(0, "Estimating cost basis for each token...")
    current_tokens = agg_tokens.index.tolist()
    for i, token_symbol in enumerate(current_tokens):
        price_progress.progress((i + 1) / len(current_tokens), f"Looking for first acquisition of {token_symbol}...")
        in_txs = sorted([tx for tx in all_txs if tx.get('tokenSymbol') == token_symbol and tx['to'].lower() in [a.lower() for a in wallet_addresses]], key=lambda x: int(x['timeStamp']))
        if in_txs:
            first_tx = in_txs[0]
            date_obj = datetime.fromtimestamp(int(first_tx['timeStamp']))
            date_str_api = date_obj.strftime('%d-%m-%Y')
            contract_address = first_tx.get('contractAddress')
            coingecko_id = get_coingecko_id_by_contract(contract_address)
            price = get_historical_price(coingecko_id, date_str_api)
            if price: cost_basis_data[token_symbol] = price
            time.sleep(1.5)
    price_progress.empty()

    pnl_results = []
    for symbol, row in agg_tokens.iterrows():
        cost_per_token = cost_basis_data.get(symbol)
        if cost_per_token and row['quote_rate'] and row['quote_rate'] > 0:
            quantity = row['total_value'] / row['quote_rate']
            estimated_cost = quantity * cost_per_token
            unrealized_pnl = row['total_value'] - estimated_cost
            pnl_results.append({'Estimated Cost Basis': estimated_cost, 'Unrealized P&L': unrealized_pnl})
        else:
            pnl_results.append({'Estimated Cost Basis': None, 'Unrealized P&L': None})
    pnl_df = pd.DataFrame(pnl_results, index=agg_tokens.index)
    return agg_tokens.join(pnl_df)

# -- Analytics Functions --

def display_token_breakdown(df, title, firm_wallets_df=None, api_key=None):
    """
    Displays aggregated token holdings and a pie chart.
    Also allows for the calculation of unrealized P&L.
    """
    if df.empty:
        st.warning("No token data available to display."); return
    agg_rules = {'total_value': ('quote', 'sum'), 'quote_rate': ('quote_rate', 'first')}
    if 'chain_name' in df.columns: agg_rules['chain'] = ('chain_name', 'first')
    agg_tokens = df.groupby('contract_ticker_symbol').agg(**agg_rules).sort_values(by='total_value', ascending=False)
    agg_tokens = agg_tokens[agg_tokens['total_value'] > 1] # Filter out dust 
    
    st.subheader("Aggregated Token Holdings")

    if firm_wallets_df is not None and not firm_wallets_df.empty and st.button("💰 Calculate Unrealized P&L (Very Slow)"):
        st.session_state.pnl_token_df = calculate_unrealized_pnl(firm_wallets_df, api_key)
    
    if 'pnl_token_df' in st.session_state and not st.session_state.pnl_token_df.empty and st.session_state.last_analyzed == selected_target:
        display_df = st.session_state.pnl_token_df
        st.dataframe(
            display_df.style.format({
                "total_value": "${:,.2f}", "quote_rate": "${:,.2f}",
                "Estimated Cost Basis": "${:,.2f}", "Unrealized P&L": "${:,.2f}"
            }),
            column_config={"total_value": "Current Value (USD)", "quote_rate": "Current Price"},
            use_container_width=True
        )
    else:
        st.dataframe(agg_tokens.style.format({"total_value": "${:,.2f}"}), column_config={"total_value": "Current Value (USD)"}, use_container_width=True)

    st.subheader("Portfolio Allocation")
    # Group small holdings (<1%) into an "Other" category for a cleaner pie chart.
    agg_tokens['percentage'] = (agg_tokens['total_value'] / agg_tokens['total_value'].sum()) * 100
    small_slices = agg_tokens[agg_tokens['percentage'] < 1]
    main_slices = agg_tokens[agg_tokens['percentage'] >= 1]
    chart_df = pd.concat([main_slices[['total_value']], pd.DataFrame([{'total_value': small_slices['total_value'].sum()}], index=['Other (<1%)'])]) if not small_slices.empty else main_slices[['total_value']]
    fig = px.pie(chart_df, names=chart_df.index, values='total_value', title=title, hole=.3)
    fig.update_traces(textposition='inside', textinfo='percent+label', hovertemplate="<b>%{label}</b><br>Value: $%{value:,.2f}<br>Percentage: %{percent:.2%}")
    st.plotly_chart(fig, use_container_width=True)

def generate_network_visualization(raw_txs, price_map, selected_address, vc_name, vc_master_list):
    """Creates a polished network graph with improved legibility.
    This is a force-directed graph that shows the relationships and transaction flows between a VC wallet and its counterparties.
    """
    st.subheader("Transaction Network Graph")

    if not raw_txs:
        st.warning("No transaction data available to generate a network graph.")
        return

    net = Network(notebook=True, cdn_resources='in_line', height="800px", width="100%", bgcolor="#222222", font_color="white")
    
    # --- Custom physics options for spacious, legible "star" layout ---
    options = """
    var options = {
      "physics": {
        "enabled": true,
        "barnesHut": {
          "gravitationalConstant": -8000,
          "centralGravity": 0.05,
          "springLength": 250,
          "springConstant": 0.05
        },
        "minVelocity": 0.75
      }
    }
    """
    net.set_options(options)
    
    # Add the main central node for the selected wallet.
    net.add_node(selected_address, label=vc_name, color='#FF4B4B', size=25, title=f"Selected Wallet:\n{selected_address}")

    # Process transactions to create counterparty nodes and the connecting edges.
    for tx in raw_txs:
        source = tx['from']
        dest = tx['to']
        
        is_transfer_out = source.lower() == selected_address.lower()
        counterparty_address = dest if is_transfer_out else source
        
        counterparty_label = get_address_label(counterparty_address, vc_master_list)
        net.add_node(counterparty_address, label=counterparty_label, title=counterparty_address, color="#00A0E5", size=15)
        
        # Enrich the edge with additional information
        token_symbol = tx.get('tokenSymbol', '???')
        price = price_map.get(token_symbol)
        token_decimals = int(tx.get('tokenDecimal', '18'))
        value_tokens = int(tx['value']) / (10**token_decimals)
        usd_value = value_tokens * price if price else None

        edge_title = f"{value_tokens:,.2f} ${token_symbol}"
        if usd_value:
            edge_title += f" (~${usd_value:,.2f})"

        # Make the line thicker for higher value transactions.
        edge_width = 1
        if usd_value:
            if usd_value > 500000: edge_width = 8
            elif usd_value > 100000: edge_width = 5
            elif usd_value > 10000: edge_width = 2
        
        # Color-code the edges based on transaction direction.
        # Red for Out, Green for In
        edge_color = '#FF6347' if is_transfer_out else '#32CD32'
        
        net.add_edge(source, dest, title=edge_title, width=edge_width, color=edge_color)

    # Save and display the graph in Streamlit.
    try:
        net.save_graph("network_graph.html")
        with open("network_graph.html", "r", encoding="utf-8") as html_file:
            source_code = html_file.read()
        components.html(source_code, height=820)
    except Exception as e:
        st.error(f"Could not display the network graph: {e}")

# --- 3. STREAMLIT APP LAYOUT ---
# Main body of the app, defining the user interface.
st.title("On-Chain VC Intelligence Dashboard")

# The master watchlist is fetched once and cached for performance.
with st.spinner("Fetching master wallet list from Dune..."):
    vc_master_list = get_dune_watchlist(DUNE_QUERY_ID, DUNE_API_KEY)

if vc_master_list is None:
    st.error("Could not load master VC watchlist.")
    st.stop()

# -- UI CONTROLS --
unique_firms = sorted([firm for firm in vc_master_list['Firm'].unique() if firm != 'Other'])
options = ["Select a target..."] + ["All VCs (Leaderboard)"] + unique_firms

st.header("1. Select a Target")
selected_target = st.selectbox("Choose a VC for a deep dive, or select 'All VCs' for a market overview:", options=options, key="selected_vc")

# -- STATE MANAGEMENT --
# This block ensures that if a new VC is selected, the previous analysis is cleared.
if 'last_analyzed' not in st.session_state or st.session_state.last_analyzed != selected_target:
    st.session_state.last_analyzed = selected_target
    st.session_state.analysis_complete = False
    if 'pnl_token_df' in st.session_state: del st.session_state.pnl_token_df

# -- Main Analysis & Display Block --
if selected_target != "Select a target...":

    # Runs the main, time-consuming API calls only when needed.
    if not st.session_state.analysis_complete:
        df_to_analyze = vc_master_list[vc_master_list['Firm'] == selected_target].copy() if selected_target != "All VCs (Leaderboard)" else vc_master_list.copy()
        
        all_portfolios, all_tokens_list = [], []
        progress_bar = st.progress(0, "Starting analysis...")
        for i, row in enumerate(df_to_analyze.itertuples()):
            
            progress_bar.progress((i+1)/len(df_to_analyze), f"Analyzing wallet {i+1}/{len(df_to_analyze)}: {row.name}")
            portfolio_data = get_covalent_portfolio(row.address, COVALENT_API_KEY)
            if portfolio_data:
                priced_items = [item for item in portfolio_data if item.get('quote') is not None and item.get('quote') > 0]
                for item in priced_items: item['Firm'] = row.Firm
                all_tokens_list.extend(priced_items)
                all_portfolios.append({"Firm": row.Firm, "Name": row.name, "Address": f"https://etherscan.io/address/{row.address}", "Value (USD)": sum(item.get('quote', 0) for item in priced_items)})
            time.sleep(0.5)
        progress_bar.empty()
        st.session_state.all_portfolios_df = pd.DataFrame(all_portfolios)
        st.session_state.all_tokens_df = pd.DataFrame(all_tokens_list)
        st.session_state.analysis_complete = True

    # -- UI DISPLAY LOGIC --

    # VIEW 1: The Leaderboard for "All VCs"
    if selected_target == "All VCs (Leaderboard)":
        st.header("2. VC Leaderboard")
        leaderboard_df = st.session_state.all_portfolios_df.groupby('Firm')['Value (USD)'].sum().sort_values(ascending=False).reset_index()
        wallet_counts = vc_master_list['Firm'].value_counts().reset_index()
        wallet_counts.columns = ['Firm', 'Wallet Count']
        leaderboard_df = pd.merge(leaderboard_df, wallet_counts, on='Firm')
        st.dataframe(
            leaderboard_df.style.format({"Value (USD)": "${:,.2f}"}),
            use_container_width=True, hide_index=True
        )
        st.header("3. Portfolio Deep Dive")
        drill_down_firm = st.selectbox("Select a firm from the leaderboard to see its portfolio breakdown:", options=["Select a firm..."] + list(leaderboard_df['Firm']))
        if drill_down_firm != "Select a firm...":
            firm_token_df = st.session_state.all_tokens_df[st.session_state.all_tokens_df['Firm'] == drill_down_firm]
            display_token_breakdown(firm_token_df, f"Asset Allocation for {drill_down_firm}")
    
    # VIEW 2: The Deep Dive for a single VC
    else:
        summary_df = st.session_state.all_portfolios_df
        token_df = st.session_state.all_tokens_df
        if summary_df.empty:
            st.warning("Could not analyze any portfolios for this firm.")
        else:
            st.header(f"2. Analysis for {selected_target}")
            st.subheader("Aggregated Portfolio Overview")
            st.metric(f"Total On-Chain Value ({selected_target})", f"${summary_df['Value (USD)'].sum():,.2f}")
            st.subheader("Wallet Breakdown")
            st.dataframe(
                summary_df.sort_values(by="Value (USD)", ascending=False).drop(columns=['Firm']).style.format({"Value (USD)": "${:,.2f}"}),
                column_config={"Address": st.column_config.LinkColumn("Etherscan Link")},
                use_container_width=True, hide_index=True
            )
            st.header("3. Token Portfolio Deep Dive")
            display_token_breakdown(token_df, f"Asset Allocation for {selected_target}", summary_df, ETHERSCAN_API_KEY)
            st.header("4. Recent Wallet Activity")
            st.info("Select a high-value wallet from the breakdown above to see its recent transactions.")
            summary_df['address_only'] = summary_df['Address'].apply(lambda x: x.split('/')[-1])
            wallet_options = ["Select a wallet..."] + [f"{row['Name']} ({row['address_only'][-6:]})" for _, row in summary_df.sort_values(by='Value (USD)', ascending=False).iterrows()]
            selected_wallet_str = st.selectbox("Select a wallet to view its activity:", options=wallet_options)
            if selected_wallet_str != "Select a wallet...":
                selected_address = summary_df[summary_df.apply(lambda row: f"{row['Name']} ({row['address_only'][-6:]})" == selected_wallet_str, axis=1)].iloc[0]['address_only']
                with st.spinner(f"Fetching transaction history for {selected_address}..."):
                    token_txs = get_token_transfers(selected_address, ETHERSCAN_API_KEY)
                if token_txs:
                    with st.spinner("Building current price map..."):
                        price_map = build_price_map_from_portfolio(token_df)
                    processed_txs = []
                    for tx in token_txs:
                        token_symbol = tx.get('tokenSymbol', '???')
                        price = price_map.get(token_symbol)
                        token_decimals = int(tx.get('tokenDecimal', '18'))
                        value_tokens = int(tx['value']) / (10**token_decimals)
                        usd_value = value_tokens * price if price else None
                        direction = "Out" if tx['from'].lower() == selected_address.lower() else "In"
                        counterparty_address = tx['to'] if direction == "Out" else tx['from']
                        counterparty_label = get_address_label(counterparty_address, vc_master_list)
                        processed_txs.append({
                            "Timestamp": datetime.fromtimestamp(int(tx['timeStamp'])).strftime('%Y-%m-%d %H:%M:%S'),
                            "Activity": f"Token Transfer ({direction})",
                            "Details": f"{value_tokens:,.2f} ${token_symbol}",
                            "Approx. USD Value (Current)": usd_value,
                            "Counterparty": counterparty_label,
                            "Tx Hash": f"https://etherscan.io/tx/{tx['hash']}"
                        })
                    activity_df = pd.DataFrame(processed_txs)
                    activity_df['sort_value'] = activity_df['Approx. USD Value (Current)'].fillna(0)
                    activity_df = activity_df.sort_values(by='sort_value', ascending=False).drop(columns=['sort_value'])
                    st.subheader(f"Recent Token Transfers (Sorted by Approx. Current Value)")
                    st.dataframe(
                        activity_df.style.format({"Approx. USD Value (Current)": "${:,.2f}"}),
                        column_config={"Tx Hash": st.column_config.LinkColumn("Etherscan")},
                        use_container_width=True,
                        hide_index=True
                    )
                    st.subheader("Transaction Network Visualizer")
                    if st.button("📊 Generate Network Graph"):
                        with st.spinner("Building network visualization..."):
                            # We can reuse the token_txs and price_map we already created
                            generate_network_visualization(token_txs, price_map, selected_address, selected_wallet_str, vc_master_list)
                else:
                    st.warning("No token transfer activity found for this address.")