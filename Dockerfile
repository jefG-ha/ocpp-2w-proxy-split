FROM python:3-slim

# replace this with your application's default port
EXPOSE 8321

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed dependencies specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Run app.py when the container launches
CMD ["python", "ocpp-2w-proxy.py"]

