from flask import Flask, redirect, request
import requests
import json
import logging

app = Flask(__name__)

# Enable debug output
logging.basicConfig(level=logging.DEBUG)

# Replace these with your app's credentials
CLIENT_ID = '37e1ce578b77a8444cc790da8dffd555'
CLIENT_SECRET = '071e3ee271fb386b96e3cf8b59a14863'
REDIRECT_URI = 'http://127.0.0.1:5000/callback'
SCOPES ='read_all_orders customer_read_customers,read_customers read_products read_reports read_product_listings read_publications PUBLIC_READ'



def fetch_data(access_token, shop, endpoint, start_date, end_date):
    base_url = f"https://{shop}.myshopify.com"
    url = base_url + endpoint

    headers = {
        "X-Shopify-Access-Token": access_token,
    }
    params = {
        "created_at_min": start_date,
        "created_at_max": end_date
    }

    # Make API request to fetch data
    response = requests.get(url, headers=headers)
    logging.debug(f"API Request URL: {response.request.url}")
    
    if response.status_code == 200:
        logging.debug(f"API Response: {response.json()}")
        return response.json()
    else:
        logging.error(f"Failed to fetch data from {endpoint}. Status Code: {response.status_code}, Error Message: {response.text}")
        return None


# Functions to calculate metrics
def calculate_aov(total_revenue, number_of_orders):
    if number_of_orders == 0:
        return 0
    return total_revenue / number_of_orders

def calculate_items_per_order(total_items_sold, number_of_orders):
    if number_of_orders == 0:
        return 0
    return total_items_sold / number_of_orders

def calculate_repeat_purchasers(total_customers, number_of_repeat_purchasers):
    if total_customers == 0:
        return 0
    return number_of_repeat_purchasers / total_customers

def calculate_new_purchasers(total_customers, number_of_new_purchasers):
    if total_customers == 0:
        return 0
    return number_of_new_purchasers / total_customers

# Route for starting the authentication flow
@app.route('/start')
def start():
    shop = 'decidable-test-store.myshopify.com'  # Replace with your shop's domain
    oauth_url = f"https://{shop}/admin/oauth/authorize?client_id={CLIENT_ID}&scope={SCOPES}&redirect_uri={REDIRECT_URI}&response_type=code"
    return redirect(oauth_url)

# Route for the callback after authentication
@app.route('/callback')
# Route for the callback after authentication
@app.route('/callback')
def callback():
    code = request.args.get('code')
    shop = request.args.get('shop')

    # Exchange authorization code for access token
    response = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        data={'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'code': code}
    )
    data = response.json()
    access_token = data.get('access_token')

    # Redirect to the /extract-data endpoint with the access token as a query parameter
    return redirect(f"/extract-data?access_token={access_token}")


# Route for extracting data and calculating metrics
@app.route('/extract-data')
def extract_data():
    # Define endpoints for required data
    endpoints = [
        "/admin/api/2022-01/reports.json?name=sales_over_time",
        "/admin/api/2022-01/reports.json?name=average_order_value",
        "/admin/api/2022-01/orders.json",
        "/admin/api/2022-01/reports.json?name=top_products_by_units_sold",
        "/admin/api/2022-01/customers/search.json?query=returning",
        "/admin/api/2022-01/customers/search.json",
        "/admin/api/2022-01/products.json",


    ]

    shop = 'decidable-test-store'  # Add the shop name
    access_token = request.args.get('access_token')  # Retrieve access token from query parameter

    start_date = '2020-01-01'  # Replace with your desired start date
    end_date = '2024-01-01'
    # Dictionary to store fetched data
    fetched_data = {}

    for endpoint in endpoints:
        data = fetch_data( access_token, shop, endpoint, start_date, end_date)
        if data:
            endpoint_key = endpoint.split('/')[-1].split('?')[0] 
            fetched_data[endpoint_key] = data
            print(f"Data fetched from {endpoint}")
        else:
            print(f"Failed to fetch data from {endpoint}. Please check your credentials and try again.")

    # Calculate metrics
    total_revenue = fetched_data.get('reports.json', {}).get('total_sales', 0)
    number_of_orders = len(fetched_data.get('orders.json', {}).get('orders', []))
    total_items_sold = fetched_data.get('orders.json', {}).get('total_items_sold', 0)
    total_customers = len(fetched_data.get('customers.json', {}).get('customers', []))
    number_of_repeat_purchasers = len(fetched_data.get('customers_repeat.json', {}).get('customers', []))
    number_of_new_purchasers = len(fetched_data.get('customers_new.json', {}).get('customers', []))

    # Calculate AOV, Items Per Order, Repeat Purchasers, and New Purchasers
    aov = calculate_aov(total_revenue, number_of_orders)
    items_per_order = calculate_items_per_order(total_items_sold, number_of_orders)
    repeat_purchasers_percentage = calculate_repeat_purchasers(total_customers, number_of_repeat_purchasers)
    new_purchasers_percentage = calculate_new_purchasers(total_customers, number_of_new_purchasers)

    # Include calculated metrics in the fetched data dictionary
    fetched_data['metrics'] = {
        'total_revenue':total_revenue,
        'number_of_orders':number_of_orders,
        'total_items_sold':total_items_sold,
        'total_customers':total_customers,
        'number_of_repeat_purchasers':number_of_repeat_purchasers,
        'number_of_new_purchasers':number_of_new_purchasers,
        'aov': aov,
        'items_per_order': items_per_order,
        'repeat_purchasers_percentage': repeat_purchasers_percentage,
        'new_purchasers_percentage': new_purchasers_percentage
    }
    # Save fetched data with metrics to a JSON file
    with open("data.json", "w") as json_file:
        json.dump(fetched_data, json_file, indent=4)
    print("Shopify data saved to 'data.json' file.")

    return "Data extraction complete."

if __name__ == "__main__":
    app.run(debug=True)
