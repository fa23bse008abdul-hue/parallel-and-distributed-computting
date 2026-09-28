
import socket
import threading

# Server settings
HOST = "127.0.0.1"
PORT = 5000

# Lock for thread synchronization
lock = threading.Lock()


def handle_client(client_socket, client_address):
    thread_name = threading.current_thread().name
    client_ip = client_address[0]
    client_port = client_address[1]

    # Lock acquire
    lock.acquire()

    try:
        print(f"\n[CONNECTED]")
        print(f"Active Thread: {thread_name}")
        print(f"Client IP: {client_ip}")
        print(f"Port: {client_port}")
    finally:
        # Lock release
        lock.release()

    try:
        while True:
            data = client_socket.recv(1024)

            if not data:
                break

            message = data.decode("utf-8")

            print(f"[{thread_name}] {client_ip}:{client_port} -> {message}")

            if message.lower() == "exit":
                break

            # Send response back to client
            response = f"Server received: {message}"
            client_socket.send(response.encode("utf-8"))

    except ConnectionResetError:
        print(f"Client {client_ip}:{client_port} disconnected unexpectedly.")

    finally:
        client_socket.close()

        lock.acquire()
        try:
            print(f"[DISCONNECTED] {client_ip}:{client_port}")
            print(f"Thread {thread_name} finished.")
        finally:
            lock.release()


# Create TCP socket
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Allow socket reuse
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

# Bind server to IP and port
server_socket.bind((HOST, PORT))

# Start listening
server_socket.listen(5)

print("====================================")
print("   Multi-Threaded TCP Server")
print("====================================")
print(f"Server running on {HOST}:{PORT}")
print("Waiting for clients...\n")


try:
    while True:
        # Accept a new client
        client_socket, client_address = server_socket.accept()

        # Create a new thread for every client
        client_thread = threading.Thread(
            target=handle_client,
            args=(client_socket, client_address)
        )

        client_thread.start()

finally:
    server_socket.close()