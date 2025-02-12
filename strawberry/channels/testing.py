from __future__ import annotations

import uuid
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

from graphql import GraphQLError, GraphQLFormattedError

from channels.testing.websocket import WebsocketCommunicator
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL
from strawberry.subscriptions.protocols.graphql_transport_ws.types import (
    ConnectionAckMessage,
    ConnectionInitMessage,
    ErrorMessage,
    NextMessage,
    SubscribeMessage,
    SubscribeMessagePayload,
)
from strawberry.subscriptions.protocols.graphql_ws.types import (
    ConnectionAckMessage as GraphQLWSConnectionAckMessage,
)
from strawberry.subscriptions.protocols.graphql_ws.types import (
    ConnectionInitMessage as GraphQLWSConnectionInitMessage,
)
from strawberry.subscriptions.protocols.graphql_ws.types import (
    StartMessage as GraphQLWSStartMessage,
)
from strawberry.types import ExecutionResult

if TYPE_CHECKING:
    from types import TracebackType
    from typing_extensions import Self

    from asgiref.typing import ASGIApplication


class GraphQLWebsocketCommunicator(WebsocketCommunicator):
    """A test communicator for GraphQL over Websockets.

    ```python
    import pytest
    from strawberry.channels.testing import GraphQLWebsocketCommunicator
    from myapp.asgi import application


    @pytest.fixture
    async def gql_communicator():
        async with GraphQLWebsocketCommunicator(application, path="/graphql") as client:
            yield client


    async def test_subscribe_echo(gql_communicator):
        async for res in gql_communicator.subscribe(
            query='subscription { echo(message: "Hi") }'
        ):
            assert res.data == {"echo": "Hi"}
    ```
    """

    def __init__(
        self,
        application: ASGIApplication,
        path: str,
        headers: Optional[List[Tuple[bytes, bytes]]] = None,
        protocol: str = GRAPHQL_TRANSPORT_WS_PROTOCOL,
        connection_params: dict = {},
        **kwargs: Any,
    ) -> None:
        """Create a new communicator.

        Args:
            application: Your asgi application that encapsulates the strawberry schema.
            path: the url endpoint for the schema.
            protocol: currently this supports `graphql-transport-ws` only.
            connection_params: a dictionary of connection parameters to send to the server.
            headers: a list of tuples to be sent as headers to the server.
            subprotocols: an ordered list of preferred subprotocols to be sent to the server.
            **kwargs: additional arguments to be passed to the `WebsocketCommunicator` constructor.
        """
        self.protocol = protocol
        subprotocols = kwargs.get("subprotocols", [])
        subprotocols.append(protocol)
        self.connection_params = connection_params
        super().__init__(application, path, headers, subprotocols=subprotocols)

    async def __aenter__(self) -> Self:
        await self.gql_init()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.disconnect()

    async def gql_init(self) -> None:
        res = await self.connect()
        if self.protocol == GRAPHQL_TRANSPORT_WS_PROTOCOL:
            assert res == (True, GRAPHQL_TRANSPORT_WS_PROTOCOL)
            await self.send_json_to(
                ConnectionInitMessage(payload=self.connection_params).as_dict()
            )
            graphql_transport_ws_response = await self.receive_json_from()
            assert graphql_transport_ws_response == ConnectionAckMessage().as_dict()
        else:
            assert res == (True, GRAPHQL_WS_PROTOCOL)
            await self.send_json_to(
                GraphQLWSConnectionInitMessage({"type": "connection_init"})
            )
            graphql_ws_response: GraphQLWSConnectionAckMessage = (
                await self.receive_json_from()
            )
            assert graphql_ws_response["type"] == "connection_ack"

    # Actual `ExecutionResult`` objects are not available client-side, since they
    # get transformed into `FormattedExecutionResult` on the wire, but we attempt
    # to do a limited representation of them here, to make testing simpler.
    async def subscribe(
        self, query: str, variables: Optional[Dict] = None
    ) -> Union[ExecutionResult, AsyncIterator[ExecutionResult]]:
        id_ = uuid.uuid4().hex

        if self.protocol == GRAPHQL_TRANSPORT_WS_PROTOCOL:
            await self.send_json_to(
                SubscribeMessage(
                    id=id_,
                    payload=SubscribeMessagePayload(query=query, variables=variables),
                ).as_dict()
            )
        else:
            start_message: GraphQLWSStartMessage = {
                "type": "start",
                "id": id_,
                "payload": {
                    "query": query,
                },
            }

            if variables is not None:
                start_message["payload"]["variables"] = variables

            await self.send_json_to(start_message)

        while True:
            response = await self.receive_json_from(timeout=5)
            message_type = response["type"]
            if message_type == NextMessage.type:
                payload = NextMessage(**response).payload
                ret = ExecutionResult(payload.get("data"), None)
                if "errors" in payload:
                    ret.errors = self.process_errors(payload.get("errors") or [])
                ret.extensions = payload.get("extensions", None)
                yield ret
            elif message_type == ErrorMessage.type:
                error_payload = ErrorMessage(**response).payload
                yield ExecutionResult(
                    data=None, errors=self.process_errors(error_payload)
                )
                return  # an error message is the last message for a subscription
            else:
                return

    def process_errors(self, errors: List[GraphQLFormattedError]) -> List[GraphQLError]:
        """Reconstructs a GraphQLError from a FormattedGraphQLError."""
        result = []
        for f_error in errors:
            error = GraphQLError(
                message=f_error["message"],
                extensions=f_error.get("extensions", None),
            )
            error.path = f_error.get("path", None)
            result.append(error)
        return result


__all__ = ["GraphQLWebsocketCommunicator"]
