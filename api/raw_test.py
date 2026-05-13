import socket

def test_raw_socket():
    req = (
        "POST /analyze HTTP/1.1\r\n"
        "Host: 127.0.0.1:8000\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 42\r\n"
        "Connection: close\r\n\r\n"
        '{"problem": "test problem", "domain": ""}'
    )
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect(('127.0.0.1', 8000))
        s.sendall(req.encode('utf-8'))
        
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            print(chunk)

if __name__ == "__main__":
    test_raw_socket()
