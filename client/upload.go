package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/textproto"
	"os"
	"time"
)

type captureMetadata struct {
	SchemaVersion int             `json:"schema_version"`
	CaptureID     string          `json:"capture_id"`
	ClientID      string          `json:"client_id"`
	ClientName    string          `json:"client_name"`
	CapturedAt    string          `json:"captured_at"`
	CaptureMode   string          `json:"capture_mode"`
	Platform      string          `json:"platform"`
	Compositor    string          `json:"compositor"`
	Image         imageMetadata   `json:"image"`
	Application   applicationMeta `json:"application"`
}

type imageMetadata struct {
	Format string `json:"format"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
}

type applicationMeta struct {
	Title string `json:"title"`
}

type uploadResponse struct {
	CaptureID        string `json:"capture_id"`
	ApplicationID    string `json:"application_id"`
	Status           string `json:"status"`
	ThumbnailStatus  string `json:"thumbnail_status"`
	IdempotentReplay bool   `json:"idempotent_replay"`
}

type errorResponse struct {
	Error struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

type idleConn struct {
	net.Conn
	idle time.Duration
}

func (connection *idleConn) Read(buffer []byte) (int, error) {
	_ = connection.SetReadDeadline(time.Now().Add(connection.idle))
	return connection.Conn.Read(buffer)
}

func (connection *idleConn) Write(buffer []byte) (int, error) {
	_ = connection.SetWriteDeadline(time.Now().Add(connection.idle))
	return connection.Conn.Write(buffer)
}

func newHTTPClient() *http.Client {
	dialer := &net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			connection, err := dialer.DialContext(ctx, network, address)
			if err != nil {
				return nil, err
			}
			return &idleConn{Conn: connection, idle: 30 * time.Second}, nil
		},
		TLSHandshakeTimeout:   5 * time.Second,
		ResponseHeaderTimeout: 15 * time.Second,
		ExpectContinueTimeout: time.Second,
	}
	return &http.Client{Transport: transport}
}

func multipartHeader(name, filename, contentType string) textproto.MIMEHeader {
	header := make(textproto.MIMEHeader)
	header.Set("Content-Disposition", fmt.Sprintf(`form-data; name=%q; filename=%q`, name, filename))
	header.Set("Content-Type", contentType)
	return header
}

func multipartBody(metadata captureMetadata, imagePath string) (io.ReadCloser, string, <-chan error, error) {
	metadataJSON, err := json.Marshal(metadata)
	if err != nil {
		return nil, "", nil, err
	}
	reader, writer := io.Pipe()
	multipartWriter := multipart.NewWriter(writer)
	contentType := multipartWriter.FormDataContentType()
	done := make(chan error, 1)
	go func() {
		var writeErr error
		defer func() {
			if writeErr == nil {
				writeErr = multipartWriter.Close()
			}
			if writeErr != nil {
				_ = writer.CloseWithError(writeErr)
			} else {
				_ = writer.Close()
			}
			done <- writeErr
		}()

		part, err := multipartWriter.CreatePart(multipartHeader("metadata", "metadata.json", "application/json"))
		if err != nil {
			writeErr = err
			return
		}
		if _, err := part.Write(metadataJSON); err != nil {
			writeErr = err
			return
		}
		image, err := os.Open(imagePath)
		if err != nil {
			writeErr = err
			return
		}
		defer image.Close()
		part, err = multipartWriter.CreatePart(multipartHeader("image", "capture.png", "image/png"))
		if err != nil {
			writeErr = err
			return
		}
		_, writeErr = io.Copy(part, image)
	}()
	return reader, contentType, done, nil
}

func uploadCapture(ctx context.Context, client *http.Client, config Config, metadata captureMetadata, imagePath string) (uploadResponse, error) {
	body, contentType, writeDone, err := multipartBody(metadata, imagePath)
	if err != nil {
		return uploadResponse{}, err
	}
	defer body.Close()
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, config.ServerURL+"/api/v1/captures", body)
	if err != nil {
		return uploadResponse{}, err
	}
	request.Header.Set("Authorization", "Bearer "+config.Token)
	request.Header.Set("Content-Type", contentType)
	response, requestErr := client.Do(request)
	writeErr := <-writeDone
	if requestErr != nil {
		return uploadResponse{}, requestErr
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusAccepted && writeErr != nil {
		return uploadResponse{}, fmt.Errorf("stream multipart request: %w", writeErr)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return uploadResponse{}, err
	}
	if response.StatusCode == http.StatusAccepted {
		var accepted uploadResponse
		if err := json.Unmarshal(data, &accepted); err != nil {
			return uploadResponse{}, fmt.Errorf("decode success response: %w", err)
		}
		if accepted.CaptureID != metadata.CaptureID {
			return uploadResponse{}, fmt.Errorf("server returned capture_id %q for %q", accepted.CaptureID, metadata.CaptureID)
		}
		return accepted, nil
	}

	var rejected errorResponse
	_ = json.Unmarshal(data, &rejected)
	message := rejected.Error.Message
	if message == "" {
		message = http.StatusText(response.StatusCode)
	}
	switch response.StatusCode {
	case http.StatusBadRequest:
		return uploadResponse{}, fmt.Errorf("server rejected the upload request: %s", message)
	case http.StatusUnauthorized:
		return uploadResponse{}, fmt.Errorf("server authentication failed")
	case http.StatusRequestEntityTooLarge:
		return uploadResponse{}, fmt.Errorf("screenshot exceeds the server upload limit")
	case http.StatusUnsupportedMediaType:
		return uploadResponse{}, fmt.Errorf("server rejected the screenshot format: %s", message)
	case http.StatusUnprocessableEntity:
		return uploadResponse{}, fmt.Errorf("server rejected the screenshot metadata or PNG: %s", message)
	default:
		return uploadResponse{}, fmt.Errorf("server returned HTTP %d: %s", response.StatusCode, message)
	}
}

func checkServer(ctx context.Context, client *http.Client, config Config) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, config.ServerURL+"/api/v1/engines", nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+config.Token)
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("server returned HTTP %d", response.StatusCode)
	}
	var value struct {
		Engines []json.RawMessage `json:"engines"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&value); err != nil {
		return err
	}
	if value.Engines == nil {
		return fmt.Errorf("server response does not contain engines")
	}
	return nil
}
