local redis_time = redis.call("TIME")
local now_ms = (tonumber(redis_time[1]) * 1000) + math.floor(tonumber(redis_time[2]) / 1000)
local lease_ms = tonumber(ARGV[1])
local concurrency_limit = tonumber(ARGV[2])
local member = ARGV[3]

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now_ms)
local existing_expiry = redis.call("ZSCORE", KEYS[1], member)
local count = redis.call("ZCARD", KEYS[1])

if existing_expiry then
    redis.call("PEXPIRE", KEYS[1], lease_ms)
    return {1, count, math.floor(tonumber(existing_expiry)), now_ms}
end

if count >= concurrency_limit then
    local earliest = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
    local retry_at_ms = now_ms + lease_ms
    if #earliest >= 2 then
        retry_at_ms = math.floor(tonumber(earliest[2]))
    end
    redis.call("PEXPIRE", KEYS[1], lease_ms)
    return {0, count, retry_at_ms, now_ms}
end

local expires_at_ms = now_ms + lease_ms
redis.call("ZADD", KEYS[1], "NX", expires_at_ms, member)
redis.call("PEXPIRE", KEYS[1], lease_ms)
count = redis.call("ZCARD", KEYS[1])
return {1, count, expires_at_ms, now_ms}
