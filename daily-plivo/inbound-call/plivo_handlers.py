"""
Plivo XML Response Handlers

This file contains the webhook handlers needed for Plivo to properly
connect calls to the Daily SIP endpoint.
"""

from fastapi import Request
from fastapi.responses import PlainTextResponse
import xml.etree.ElementTree as ET


# In-memory storage for call mapping (use Redis or database in production)
call_to_sip_mapping = {}


def store_call_mapping(call_id: str, sip_uri: str, phone_number: str):
    """
    Store the mapping between call ID and SIP URI.
    In production, use a database or Redis for this.
    """
    call_to_sip_mapping[call_id] = {
        "sip_uri": sip_uri,
        "phone_number": phone_number
    }


def get_call_mapping(call_uuid: str):
    """
    Retrieve the mapping for a call.
    """
    return call_to_sip_mapping.get(call_uuid)


# Add these endpoints to your FastAPI app

async def plivo_answer_handler(request: Request):
    """
    Plivo calls this endpoint when an outbound call is answered.
    We return XML that instructs Plivo to connect to Daily's SIP endpoint.
    
    This is the answer_url that you set when creating the call.
    """
    try:
        # Get form data from Plivo
        form_data = await request.form()
        
        # Extract call information
        call_uuid = form_data.get("CallUUID")
        from_number = form_data.get("From")
        to_number = form_data.get("To")
        call_status = form_data.get("CallStatus")
        
        # Also check for call_id in query parameters (we pass it in the URL)
        call_id = request.query_params.get("call_id")
        
        print(f"=== Plivo Answer Webhook ===")
        print(f"CallUUID: {call_uuid}")
        print(f"CallID (query): {call_id}")
        print(f"From: {from_number}")
        print(f"To: {to_number}")
        print(f"Status: {call_status}")
        print(f"All form data: {dict(form_data)}")
        print(f"All mappings in storage: {call_to_sip_mapping}")
        print(f"Mapping keys: {list(call_to_sip_mapping.keys())}")
        
        # Retrieve the SIP URI for this call (try call_id first, then call_uuid, then phone number)
        mapping = None
        
        # Try by call_id (from query params)
        if call_id:
            mapping = get_call_mapping(call_id)
            print(f"Tried call_id '{call_id}', mapping: {mapping}")
        
        # Try by call_uuid (from Plivo webhook)
        if not mapping and call_uuid:
            mapping = get_call_mapping(call_uuid)
            print(f"Tried call_uuid '{call_uuid}', mapping: {mapping}")
        
        # Try by phone number (to_number from webhook) - with and without +
        if not mapping and to_number:
            mapping = get_call_mapping(to_number)
            print(f"Tried phone number '{to_number}', mapping: {mapping}")
            
            if not mapping:
                to_no_plus = to_number.lstrip('+')
                mapping = get_call_mapping(to_no_plus)
                print(f"Tried phone number without + '{to_no_plus}', mapping: {mapping}")
        
        # Last resort: search by phone number in values
        if not mapping:
            print(f"Searching all mappings by phone number...")
            for key, value in call_to_sip_mapping.items():
                stored_phone = value.get("phone_number", "")
                print(f"  Checking key '{key}' -> phone '{stored_phone}' vs '{to_number}'")
                if stored_phone == to_number or stored_phone == to_number.lstrip('+') or stored_phone.lstrip('+') == to_number:
                    mapping = value
                    print(f"Found mapping by searching phone number in values: key='{key}'")
                    break
        
        if not mapping:
            # If we don't have the mapping, return an error response
            print(f"ERROR: No mapping found for call. CallUUID: {call_uuid}, CallID: {call_id}")
            print(f"Available mappings: {list(call_to_sip_mapping.keys())}")
            print(f"All mappings: {call_to_sip_mapping}")
            error_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, there was an error connecting your call.</Speak>
    <Hangup/>
</Response>"""
            return PlainTextResponse(error_xml, media_type="application/xml")
        
        sip_uri = mapping["sip_uri"]
        print(f"Using SIP URI: {sip_uri}")
        
        # Create Plivo XML to connect to Daily's SIP endpoint
        # Using the <User> element to dial a SIP URI
        xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{from_number}" timeout="30">
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
        
        print(f"Returning XML: {xml_response}")
        return PlainTextResponse(xml_response, media_type="application/xml")
    except Exception as e:
        import traceback
        error_msg = f"Error in plivo_answer_handler: {str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        # Return a valid XML response even on error
        error_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>Sorry, there was an error connecting your call.</Speak>
    <Hangup/>
</Response>"""
        return PlainTextResponse(error_xml, media_type="application/xml")


async def plivo_hangup_handler(request: Request):
    """
    Plivo calls this endpoint when a call ends.
    This is the hangup_url that you can optionally set.
    """
    form_data = await request.form()
    
    call_uuid = form_data.get("CallUUID")
    hangup_cause = form_data.get("HangupCause")
    duration = form_data.get("Duration")
    
    print(f"Call ended - CallUUID: {call_uuid}, Cause: {hangup_cause}, Duration: {duration}s")
    
    # Clean up the mapping
    if call_uuid in call_to_sip_mapping:
        del call_to_sip_mapping[call_uuid]
    
    # You don't need to return anything for hangup webhooks
    return {"status": "received"}


async def plivo_fallback_handler(request: Request):
    """
    Plivo calls this endpoint if the answer_url fails.
    This is the fallback_answer_url that you can optionally set.
    """
    form_data = await request.form()
    
    call_uuid = form_data.get("CallUUID")
    
    print(f"Fallback triggered for CallUUID: {call_uuid}")
    
    # Return a simple error message
    fallback_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>We're sorry, but we're unable to connect your call at this time. Please try again later.</Speak>
    <Hangup/>
</Response>"""
    
    return PlainTextResponse(fallback_xml, media_type="application/xml")


# Alternative: Using Plivo's XML builder
def create_plivo_dial_xml(sip_uri: str, from_number: str):
    """
    Create Plivo XML using ElementTree (more programmatic approach)
    """
    response = ET.Element("Response")
    
    dial = ET.SubElement(response, "Dial")
    dial.set("callerId", from_number)
    
    user = ET.SubElement(dial, "User")
    user.text = sip_uri
    
    # Convert to string
    xml_string = ET.tostring(response, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_string}'


# Example of more complex XML responses

def create_xml_with_recording(sip_uri: str, from_number: str):
    """
    Example: Connect to SIP and record the call
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{from_number}" record="true" recordFileFormat="mp3">
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
    return xml


def create_xml_with_timeout(sip_uri: str, from_number: str, timeout: int = 30):
    """
    Example: Connect to SIP with a timeout
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{from_number}" timeout="{timeout}">
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
    return xml


def create_xml_with_caller_name(sip_uri: str, from_number: str, caller_name: str):
    """
    Example: Connect to SIP with a custom caller name
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{from_number}" callerName="{caller_name}">
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
    return xml


def create_xml_with_speak_before_dial(sip_uri: str, from_number: str, message: str):
    """
    Example: Play a message before connecting
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Speak>{message}</Speak>
    <Dial callerId="{from_number}">
        <User>{sip_uri}</User>
    </Dial>
</Response>"""
    return xml


# Usage in your server.py:
"""
from plivo_handlers import (
    plivo_answer_handler,
    plivo_hangup_handler,
    plivo_fallback_handler,
    store_call_mapping
)

# Add to your FastAPI app:

@app.post("/plivo-answer")
async def handle_plivo_answer(request: Request):
    return await plivo_answer_handler(request)

@app.post("/plivo-hangup")
async def handle_plivo_hangup(request: Request):
    return await plivo_hangup_handler(request)

@app.post("/plivo-fallback")
async def handle_plivo_fallback(request: Request):
    return await plivo_fallback_handler(request)

# When making a call, store the mapping:
# In your handle_outbound_call function, after getting the SIP URI:

call_id = generate_unique_id()  # Generate a unique ID
store_call_mapping(call_id, sip_uri, phone_number)

# Then when creating the Plivo call, use your webhook URLs:
response = plivo_client.calls.create(
    from_=from_number,
    to_=phone_number,
    answer_url="https://your-domain.com/plivo-answer",
    answer_method="POST",
    hangup_url="https://your-domain.com/plivo-hangup",
    hangup_method="POST",
    fallback_answer_url="https://your-domain.com/plivo-fallback",
    fallback_method="POST"
)
"""
