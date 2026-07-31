local redis_time = redis.call("TIME")
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local window_ms = tonumber(ARGV[1])
local request_limit = tonumber(ARGV[2])
local member = ARGV[3]
local cutoff_ms = now_ms - window_ms

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", cutoff_ms)
local count = redis.call("ZCARD", KEYS[1])
local allowed = 0

if count < request_limit then
    redis.call("ZADD", KEYS[1], now_ms, member)
    count = redis.call("ZCARD", KEYS[1])
    allowed = 1
end

redis.call("PEXPIRE", KEYS[1], window_ms)
local earliest = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
local reset_at_ms = now_ms + window_ms
if #earliest >= 2 then
    reset_at_ms = math.floor(tonumber(earliest[2]) + window_ms)
end

return {allowed, count, reset_at_ms, now_ms}
