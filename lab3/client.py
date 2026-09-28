
import socket

# Server settings
HOST = "127.0.0.1"
PORT = 5000

# Create TCP socket
client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Connect to server
client_socket.connect((HOST, PORT))

print("====================================")
print("       TCP Client")
print("====================================")
print("Connected to server.")
print("Type 'exit' to disconnect.\n")


try:
    while True:
        # Get message from user
        message = input("You: ")

        # Send message to server
        client_socket.send(message.encode("utf-8"))

        # Exit condition
        if message.lower() == "exit":
            break

        # Receive server response
        response = client_socket.recv(1024).decode("utf-8")

        print(f"Server: {response}")

finally:
    client_socket.close()
    print("Disconnected from server.")