import os
import sys
import argparse
import subprocess
import uuid
import ssl
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from plivo_handlers import (
    plivo_answer_handler,
    plivo_hangup_handler,
    plivo_fallback_handler,
    store_call_mapping,
    call_to_sip_mapping
)

load_dotenv(override=True)

# Daily configuration
DAILY_API_KEY = os.getenv("DAILY_API_KEY")
DAILY_API_URL = os.getenv("DAILY_API_URL", "https://api.daily.co/v1")

# Plivo configuration
PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID")
PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")

# Store bot subprocess
bot_procs = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Clean up bot processes
    for proc in bot_procs:
        proc.terminate()
        proc.wait()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "message": "Plivo SIP Dial-in Server",
        "endpoints": {
            "inbound_call": "/plivo-inbound",
            "plivo_answer": "/plivo-answer",
            "plivo_hangup": "/plivo-hangup",
            "plivo_fallback": "/plivo-fallback"
        }
    }


@app.post("/plivo-answer")
async def handle_plivo_answer(request: Request):
    """Plivo webhook for when a call is answered"""
    return await plivo_answer_handler(request)


@app.post("/plivo-hangup")
async def handle_plivo_hangup(request: Request):
    """Plivo webhook for when a call ends"""
    return await plivo_hangup_handler(request)


@app.post("/plivo-fallback")
async def handle_plivo_fallback(request: Request):
    """Plivo webhook fallback if answer_url fails"""
    return await plivo_fallback_handler(request)


@app.post("/store-call-mapping")
async def store_call_mapping_endpoint(request: Request):
    """Store call mapping for Plivo webhooks"""
    try:
        data = await request.json()
        call_id = data.get("call_id")
        sip_uri = data.get("sip_uri")
        phone_number = data.get("phone_number")
        
        if not all([call_id, sip_uri, phone_number]):
            raise HTTPException(status_code=400, detail="Missing required fields")
        
        store_call_mapping(call_id, sip_uri, phone_number)
        return {"status": "success", "call_id": call_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/plivo-inbound")
async def handle_inbound_call(request: Request):
    """
    Handles incoming calls to the Plivo phone number.
    This endpoint is called by Plivo when someone calls your Plivo number.
    
    Plivo sends form data with:
    - CallUUID: Unique identifier for the call
    - From: Caller's phone number
    - To: Your Plivo phone number
    - CallStatus: Status of the call
    """
    try:
        # Get form data from Plivo webhook
        form_data = await request.form()
        
        call_uuid = form_data.get("CallUUID")
        from_number = form_data.get("From")  # Caller's phone number
        to_number = form_data.get("To")  # Your Plivo phone number
        call_status = form_data.get("CallStatus")
        
        print(f"=== Inbound Call Received ===")
        print(f"CallUUID: {call_uuid}")
        print(f"From: {from_number}")
        print(f"To: {to_number}")
        print(f"Status: {call_status}")
        
        if not call_uuid:
            # Return error XML if we don't have a call UUID
            error_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, there was an error processing your call.</Speak>
    <Hangup/>
</Response>"""
            return PlainTextResponse(error_xml, media_type="application/xml")
        
        # Create Daily room with dial-in SIP capabilities
        room_response = await create_daily_room()
        
        if not room_response:
            error_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, we're unable to connect your call at this time. Please try again later.</Speak>
    <Hangup/>
</Response>"""
            return PlainTextResponse(error_xml, media_type="application/xml")
        
        room_url = room_response["url"]
        room_name = room_response["name"]
        token_result = await create_daily_token(room_name)
        
        if isinstance(token_result, dict) and "error" in token_result:
            error_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, there was an error setting up your call.</Speak>
    <Hangup/>
</Response>"""
            return PlainTextResponse(error_xml, media_type="application/xml")
        
        if not token_result:
            error_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, there was an error setting up your call.</Speak>
    <Hangup/>
</Response>"""
            return PlainTextResponse(error_xml, media_type="application/xml")
        
        token = token_result
        
        # Get Daily SIP URI from room configuration
        sip_uri = await get_daily_sip_uri(room_name)
        if not sip_uri:
            error_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, we're unable to connect your call. Please try again later.</Speak>
    <Hangup/>
</Response>"""
            return PlainTextResponse(error_xml, media_type="application/xml")
        
        # Store the mapping for this call
        # We'll use call_uuid as the key since that's what Plivo sends in webhooks
        store_call_mapping(call_uuid, sip_uri, from_number)
        # Also store by phone number (normalized)
        if from_number:
            store_call_mapping(from_number, sip_uri, from_number)
            phone_no_plus = from_number.lstrip('+')
            store_call_mapping(phone_no_plus, sip_uri, from_number)
        
        print(f"Stored mapping for call_uuid: {call_uuid}, from: {from_number}")
        print(f"SIP URI: {sip_uri}")
        
        # Start the bot process in the background
        try:
            # Get the directory where this script is located
            script_dir = os.path.dirname(os.path.abspath(__file__))
            bot_path = os.path.join(script_dir, "bot.py")
            
            # Ensure the bot file exists
            if not os.path.exists(bot_path):
                print(f"Warning: Bot file not found: {bot_path}")
            else:
                # Start bot process with logs written to a file
                log_file_path = os.path.join(script_dir, f"bot_{room_name}.log")
                log_file = open(log_file_path, "w")
                
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        bot_path,
                        "-u", room_url,
                        "-t", token,
                        "-n", from_number or "unknown",
                        "-s", sip_uri
                    ],
                    cwd=script_dir,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy()
                )
                bot_procs.append(proc)
                print(f"Bot process started (PID: {proc.pid})")
                print(f"Bot logs: {log_file_path}")
                
                # Give bot a moment to start (but don't wait too long - Plivo expects quick response)
                import asyncio
                await asyncio.sleep(2)  # Wait 2 seconds for bot to start
            
        except Exception as e:
            print(f"Warning: Failed to start bot process: {str(e)}")
            # Continue anyway - we'll still return the SIP connection XML
        
        # Return Plivo XML to connect the call to Daily's SIP endpoint
        # Use the caller's number as callerId
        caller_id = from_number or to_number
        
        xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{caller_id}" timeout="30">
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
        
        print(f"Returning Plivo XML to connect call to Daily SIP endpoint")
        return PlainTextResponse(xml_response, media_type="application/xml")
            
    except Exception as e:
        import traceback
        error_msg = f"Error in handle_inbound_call: {str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        # Return a valid XML response even on error
        error_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, there was an error processing your call. Please try again later.</Speak>
    <Hangup/>
</Response>"""
        return PlainTextResponse(error_xml, media_type="application/xml")


async def create_daily_room():
    """Create a Daily room configured for dial-in"""
    import aiohttp
    import certifi
    
    # Create SSL context that uses certifi certificates
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DAILY_API_KEY}"
    }
    
    data = {
        "properties": {
            "enable_dialout": True,  # Enable dial-out from within the room
            "sip": {
                "display_name": "AI Phone Bot",
                "video": False,
                "sip_mode": "dial-in"
            }
        }
    }
    
    # Create connector with SSL context
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(
            f"{DAILY_API_URL}/rooms",
            headers=headers,
            json=data
        ) as response:
            if response.status == 200:
                return await response.json()
            else:
                print(f"Failed to create Daily room: {response.status}")
                print(await response.text())
                return None


async def create_daily_token(room_name: str):
    """Create a Daily token for the room"""
    import aiohttp
    import certifi
    
    # Create SSL context that uses certifi certificates
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DAILY_API_KEY}"
    }
    
    data = {
        "properties": {
            "room_name": room_name,
            "is_owner": True
        }
    }
    
    # Create connector with SSL context
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(
            f"{DAILY_API_URL}/meeting-tokens",
            headers=headers,
            json=data
        ) as response:
            if response.status == 200:
                result = await response.json()
                return result.get("token")
            else:
                error_text = await response.text()
                print(f"Failed to create token: {response.status}")
                print(f"Error response: {error_text}")
                return {"error": f"HTTP {response.status}: {error_text}"}


async def get_daily_sip_uri(room_name: str):
    """Get the SIP URI for a Daily room"""
    import aiohttp
    import certifi
    
    # Create SSL context that uses certifi certificates
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    
    headers = {
        "Authorization": f"Bearer {DAILY_API_KEY}"
    }
    
    # Create connector with SSL context
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(
            f"{DAILY_API_URL}/rooms/{room_name}",
            headers=headers
        ) as response:
            if response.status == 200:
                result = await response.json()
                # Daily SIP URI format for dial-in: sip:room_name@domain.daily.co
                # Try to get domain from room config, or use default
                config = result.get("config", {})
                sip_config = config.get("sip", {})
                
                # Extract domain from Daily account (usually in the format: account.daily.co)
                # For now, use a default format - Daily typically uses: room_name@domain.daily.co
                # The domain part might be account-specific, but for dial-in it's usually:
                # sip:room_name@domain.daily.co or sip:room_name@domain
                
                # Daily provides the SIP endpoint in config.sip_uri.endpoint
                config = result.get("config", {})
                sip_uri_config = config.get("sip_uri", {})
                sip_endpoint = sip_uri_config.get("endpoint")
                
                if sip_endpoint:
                    # Daily provides the full SIP endpoint, use it directly
                    # Format is typically: room_name.0@daily-xxx-app.dapp.signalwire.com
                    sip_uri = f"sip:{sip_endpoint}"
                    print(f"Using Daily-provided SIP endpoint: {sip_uri}")
                else:
                    # Fallback: construct from room name (shouldn't happen if SIP is enabled)
                    print(f"WARNING: No SIP endpoint found in room config, using fallback")
                    print(f"Room config: {config}")
                    sip_uri = f"sip:{room_name}@sip.daily.co"
                
                print(f"Room name: {room_name}")
                print(f"SIP URI config: {sip_uri_config}")
                print(f"Final SIP URI: {sip_uri}")
                return sip_uri
            else:
                error_text = await response.text()
                print(f"Failed to get room info: {response.status}")
                print(f"Error response: {error_text}")
                return None


if __name__ == "__main__":
    import uvicorn
    
    parser = argparse.ArgumentParser(description="Daily Direct Dial-out Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    
    args = parser.parse_args()
    
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )