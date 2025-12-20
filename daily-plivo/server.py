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
        "message": "Plivo SIP Dial-out Server",
        "endpoints": {
            "outbound_call": "/outbound-call",
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


@app.post("/outbound-call")
async def handle_outbound_call(request: Request):
    """
    Initiates an outbound call using Daily's direct dial-out.
    
    Expected JSON body:
    {
        "phone_number": "+1234567890"
    }
    """
    try:
        data = await request.json()
        
        phone_number = data.get("phone_number")
        
        if not phone_number:
            raise HTTPException(
                status_code=400,
                detail="Missing required parameter: phone_number"
            )
        
        # Create Daily room with dial-in SIP capabilities
        room_response = await create_daily_room()
        
        if not room_response:
            raise HTTPException(
                status_code=500,
                detail="Failed to create Daily room"
            )
        
        room_url = room_response["url"]
        room_name = room_response["name"]
        token_result = await create_daily_token(room_name)
        
        if isinstance(token_result, dict) and "error" in token_result:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create Daily token: {token_result['error']}"
            )
        
        if not token_result:
            raise HTTPException(
                status_code=500,
                detail="Failed to create Daily token: Unknown error"
            )
        
        token = token_result
        
        # Get Daily SIP URI from room configuration
        sip_uri = await get_daily_sip_uri(room_name)
        if not sip_uri:
            raise HTTPException(
                status_code=500,
                detail="Failed to get Daily SIP URI"
            )
        
        # Start the bot process FIRST so it's ready when the call comes in
        try:
            # Get the directory where this script is located
            script_dir = os.path.dirname(os.path.abspath(__file__))
            bot_path = os.path.join(script_dir, "bot.py")
            
            # Ensure the bot file exists
            if not os.path.exists(bot_path):
                raise HTTPException(
                    status_code=500,
                    detail=f"Bot file not found: {bot_path}"
                )
            
            # Start bot process with logs written to a file
            log_file_path = os.path.join(script_dir, f"bot_{room_name}.log")
            log_file = open(log_file_path, "w")
            
            proc = subprocess.Popen(
                [
                    sys.executable,
                    bot_path,
                    "-u", room_url,
                    "-t", token,
                    "-n", phone_number,
                    "-s", sip_uri
                ],
                cwd=script_dir,
                stdout=log_file,
                stderr=subprocess.STDOUT,  # Combine stderr with stdout
                env=os.environ.copy()
            )
            bot_procs.append(proc)
            print(f"Bot process started (PID: {proc.pid}), waiting for it to join room...")
            print(f"Bot logs are being written to: {log_file_path}")
            print(f"To view logs in real-time, run: tail -f {log_file_path}")
            # Give bot time to join the room before making the call
            import asyncio
            await asyncio.sleep(5)  # Wait 5 seconds for bot to join and be ready
            print(f"Bot should be in room now, making Plivo call...")
            
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to start bot process: {str(e)}"
            )
        
        # Make the Plivo call from the server (after bot is in room)
        plivo_auth_id = os.getenv("PLIVO_AUTH_ID")
        plivo_auth_token = os.getenv("PLIVO_AUTH_TOKEN")
        from_number = os.getenv("PLIVO_PHONE_NUMBER")
        
        if not all([plivo_auth_id, plivo_auth_token, from_number]):
            raise HTTPException(
                status_code=500,
                detail="Plivo credentials not configured"
            )
        
        try:
            import plivo
            plivo_client = plivo.RestClient(plivo_auth_id, plivo_auth_token)
            
            # Make the Plivo call
            response = plivo_client.calls.create(
                from_=from_number,
                to_=phone_number,
                answer_url=f"{SERVER_URL}/plivo-answer",
                answer_method="POST",
                hangup_url=f"{SERVER_URL}/plivo-hangup",
                hangup_method="POST"
            )
            
            # Plivo response is a dict-like object, try different ways to get the UUID
            call_uuid = None
            if hasattr(response, 'request_uuid'):
                call_uuid = response.request_uuid
            elif hasattr(response, 'call_uuid'):
                call_uuid = response.call_uuid
            elif hasattr(response, 'message_uuid'):
                call_uuid = response.message_uuid
            elif isinstance(response, dict):
                call_uuid = response.get('request_uuid') or response.get('call_uuid') or response.get('message_uuid')
            
            print(f"Plivo call response: {response}")
            print(f"Plivo call response type: {type(response)}")
            if hasattr(response, '__dict__'):
                print(f"Plivo response attributes: {vars(response)}")
            
            # IMPORTANT: Store mapping by phone number as well, since Plivo webhook uses CallUUID
            # which might be different from request_uuid. We'll store multiple keys.
            print(f"=== Storing Call Mapping ===")
            print(f"Phone number: {phone_number}")
            print(f"SIP URI: {sip_uri}")
            
            # Store by phone number (normalized - with and without +)
            store_call_mapping(phone_number, sip_uri, phone_number)
            # Also store without + prefix
            phone_no_plus = phone_number.lstrip('+')
            store_call_mapping(phone_no_plus, sip_uri, phone_number)
            
            # Also store by call UUID if we have it
            if call_uuid:
                store_call_mapping(call_uuid, sip_uri, phone_number)
                print(f"Stored mapping for call_uuid: {call_uuid}")
            
            print(f"Stored mapping for phone_number: {phone_number} and {phone_no_plus}")
            # Import here to avoid circular import
            from plivo_handlers import call_to_sip_mapping
            print(f"All stored mappings: {call_to_sip_mapping}")
            print(f"Plivo call initiated: {response}, CallUUID: {call_uuid}")
            
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to initiate Plivo call: {str(e)}"
            )
        
        return {
            "status": "success",
            "message": f"Outbound call initiated to {phone_number}",
            "room_url": room_url,
            "room_name": room_name
        }
            
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing outbound call: {str(e)}"
        )


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