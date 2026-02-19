#!/usr/bin/env python3
"""
Test script for Multi-Agent Relay Server
Usage: python test_connection.py wss://your-relay-url.com
"""

import asyncio
import websockets
import json
import sys
from datetime import datetime

async def test_relay(url):
    """Test connection to relay server"""
    print("=" * 60)
    print("Multi-Agent Relay Connection Test")
    print("=" * 60)
    print(f"Testing: {url}")
    print()
    
    try:
        print("[1/6] Connecting...")
        async with websockets.connect(url) as websocket:
            print("✅ Connected!")
            
            # Test 1: HELLO/WELCOME
            print("\n[2/6] Testing HELLO/WELCOME handshake...")
            hello = {
                "protocol_version": "0.3",
                "message_type": "HELLO",
                "sender": "test_client",
                "capabilities": {"test": True}
            }
            await websocket.send(json.dumps(hello))
            
            welcome_raw = await websocket.recv()
            welcome = json.loads(welcome_raw)
            
            if welcome.get("message_type") == "WELCOME":
                print("✅ WELCOME received!")
                print(f"   Session ID: {welcome.get('session_id')}")
                print(f"   Connected agents: {welcome.get('connected_agents')}")
                print(f"   Server capabilities: {welcome.get('server_capabilities')}")
            else:
                print(f"❌ Expected WELCOME, got {welcome.get('message_type')}")
                return False
            
            # Test 2: PING/PONG
            print("\n[3/6] Testing PING/PONG...")
            ping = {
                "protocol_version": "0.3",
                "message_type": "PING"
            }
            await websocket.send(json.dumps(ping))
            
            pong_raw = await websocket.recv()
            pong = json.loads(pong_raw)
            
            if pong.get("message_type") == "PONG":
                print("✅ PONG received!")
            else:
                print(f"❌ Expected PONG, got {pong.get('message_type')}")
                return False
            
            # Test 3: Send MESSAGE
            print("\n[4/6] Testing MESSAGE send...")
            message = {
                "protocol_version": "0.3",
                "message_type": "MESSAGE",
                "message_id": f"test-{int(datetime.now().timestamp())}",
                "sender": "test_client",
                "content": "Test message from connection tester",
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            await websocket.send(json.dumps(message))
            
            ack_raw = await websocket.recv()
            ack = json.loads(ack_raw)
            
            if ack.get("message_type") == "ACK":
                print("✅ ACK received!")
                print(f"   Message ID: {ack.get('message_id')}")
            else:
                print(f"❌ Expected ACK, got {ack.get('message_type')}")
                return False
            
            # Test 4: Request HISTORY
            print("\n[5/6] Testing HISTORY retrieval...")
            history_req = {
                "protocol_version": "0.3",
                "message_type": "REQUEST_HISTORY"
            }
            await websocket.send(json.dumps(history_req))
            
            history_raw = await websocket.recv()
            history = json.loads(history_raw)
            
            if history.get("message_type") == "HISTORY_RESPONSE":
                print("✅ HISTORY received!")
                print(f"   Messages in history: {len(history.get('messages', []))}")
                if history.get('messages'):
                    print(f"   Latest message: {history['messages'][-1].get('content', '')[:50]}...")
            else:
                print(f"❌ Expected HISTORY_RESPONSE, got {history.get('message_type')}")
                return False
            
            # Test 5: GOODBYE
            print("\n[6/6] Testing graceful disconnect...")
            goodbye = {
                "protocol_version": "0.3",
                "message_type": "GOODBYE"
            }
            await websocket.send(json.dumps(goodbye))
            print("✅ GOODBYE sent!")
            
            print("\n" + "=" * 60)
            print("🎉 ALL TESTS PASSED!")
            print("=" * 60)
            print("\nRelay server is working correctly!")
            print(f"URL: {url}")
            print("\nYou can now use this relay for agent communication.")
            return True
            
    except websockets.exceptions.InvalidURI:
        print(f"❌ Invalid URL: {url}")
        print("   Make sure to use wss:// for HTTPS or ws:// for HTTP")
        return False
    except websockets.exceptions.InvalidStatusCode as e:
        print(f"❌ Connection failed with status {e.status_code}")
        print("   Server might not be running or URL is incorrect")
        return False
    except ConnectionRefusedError:
        print("❌ Connection refused")
        print("   Server might not be running or firewall is blocking")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python test_connection.py wss://your-relay-url.com")
        print("\nExamples:")
        print("  python test_connection.py wss://my-relay.railway.app")
        print("  python test_connection.py wss://my-relay.onrender.com")
        print("  python test_connection.py wss://my-relay.fly.dev")
        sys.exit(1)
    
    url = sys.argv[1]
    
    # Run test
    success = asyncio.run(test_relay(url))
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
