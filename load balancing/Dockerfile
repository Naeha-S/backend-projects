FROM python:3.12-slim

WORKDIR /app

# Copy the standalone load balancer
COPY load_balancer.py .

# Load balancer default port
EXPOSE 8080

# Run in unbuffered mode so logs appear immediately
ENV PYTHONUNBUFFERED=1

# Run the load balancer, binding to 0.0.0.0 to allow external connections in docker
ENTRYPOINT ["python", "load_balancer.py", "--host", "0.0.0.0"]
