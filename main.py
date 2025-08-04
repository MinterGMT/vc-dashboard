import os
import sys
import requests
import pandas as pd
from dotenv import load_dotenv
from dune_client.client import DuneClient

# --- CONFIGURATION ---
load_dotenv()
DUNE_API_KEY = os.getenv("DUNE_API_KEY")
COVALENT_API_KEY = os.getenv("COVALENT_API_KEY")

DUNE_QUERY_ID = 5551211 

# --- FUNCTIONS ---

def get_dune_watchlist(query_id, api_key):
    """
    Initializes the Dune client and fetches the latest results from our saved query.
    This version uses the get_latest_result() method, which is compatible with the free tier.
    """
    print("Step 1: Fetching VC watchlist from Dune...")
    try:
        dune = DuneClient(api_key)
        # 1. Fetch the raw query results (free tier compatible)
        query_result = dune.get_latest_result(query_id)
        print("  > Raw results fetched from Dune successfully.")
        
        # 2. Convert the raw results into a pandas DataFrame
        results_df = pd.DataFrame(query_result.result.rows)
        print(f"  > Processed {len(results_df)} rows into a DataFrame.")
        return results_df

    except Exception as e:
        print(f"  > An error occurred with the Dune client: {e}")
        return None

def get_covalent_portfolio(wallet_address, api_key):
    """Fetches the token portfolio for a wallet using the Covalent API."""
    print(f"\nStep 2: Fetching portfolio for address: {wallet_address} with Covalent...")
    chain_name = 'eth-mainnet'
    url = f"https://api.covalenthq.com/v1/{chain_name}/address/{wallet_address}/balances_v2/"
    params = {'key': api_key}
    
    response = requests.get(url, params=params)
    if response.status_code == 200:
        print("  > Covalent API call successful.")
        data = response.json().get('data', {})
        return data.get('items', [])
    else:
        print(f"  > Error fetching from Covalent API: {response.status_code} - {response.text}")
        return []

# --- MAIN EXECUTION ---

if __name__ == "__main__":
    print("--- Starting On-Chain VC Intelligence Script ---")
    
    # Pre-flight checks
    if not DUNE_API_KEY or not COVALENT_API_KEY or DUNE_QUERY_ID == 0:
        print("Error: Make sure DUNE_API_KEY, COVALENT_API_KEY, and DUNE_QUERY_ID are set correctly.")
        sys.exit(1)

    print("  > Configuration checks passed.")
    
    # 1. Get the watchlist using our corrected Dune function
    vc_watchlist_df = get_dune_watchlist(DUNE_QUERY_ID, DUNE_API_KEY)
    
    if vc_watchlist_df is not None and not vc_watchlist_df.empty:
        print(f"\nSuccessfully fetched {len(vc_watchlist_df)} addresses from Dune.")
        
        first_vc_wallet = vc_watchlist_df.iloc[0]
        vc_name = first_vc_wallet['name']
        vc_address = first_vc_wallet['address']
        
        print(f"  > Proceeding with analysis for '{vc_name}' ({vc_address}).")
        
        # 2. Fetch its portfolio from Covalent
        portfolio_data = get_covalent_portfolio(vc_address, COVALENT_API_KEY)
        
        if portfolio_data:
            # 1. Create a new list containing only tokens that have a valid price (quote is not None)
            priced_portfolio = [item for item in portfolio_data if item.get('quote') is not None]
            
            # 2. Calculate total value using this cleaned list
            total_value = sum(item.get('quote', 0) for item in priced_portfolio)
            
            print("\n--- Portfolio Details ---")
            print(f"Total Portfolio Value (USD): ${total_value:,.2f}")
            
            if total_value > 0:
                print("\nTop 5 Tokens by Value:")
                # 3. Sort the CLEANED list. This will no longer crash.
                sorted_tokens = sorted(priced_portfolio, key=lambda x: x.get('quote', 0), reverse=True)
                
                # 4. Loop through the sorted, clean list
                for token in sorted_tokens[:5]:
                    value = token.get('quote', 0)
                    symbol = token.get('contract_ticker_symbol', 'N/A')
                    print(f"  - {symbol:<10} | Value: ${value:,.2f}")
            
            # --- END OF THE FIX ---
        else:
            print("\nNo portfolio data returned from Covalent for this address.")
    else:
        print("\nCould not retrieve VC watchlist from Dune. Exiting.")